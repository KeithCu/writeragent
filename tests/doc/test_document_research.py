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
    NEARBY_IMAGE_EXTENSIONS,
    _normalize_file_url,
    _path_to_file_url,
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
    assert guess_doc_type_from_path("/tmp/Budget_2026.xlsx") == "calc"
    assert guess_doc_type_from_path("/tmp/Report.odt") == "writer"
    assert guess_doc_type_from_path("/tmp/Report.docx") == "writer"
    assert guess_doc_type_from_path("/tmp/Slides.odp") == "draw"
    assert guess_doc_type_from_path("/tmp/Slides.pptx") == "draw"
    assert guess_doc_type_from_path("/tmp/photo.png") == "image"
    assert guess_doc_type_from_path("/tmp/readme.txt") == "writer"


def test_path_to_file_url_uses_three_slashes_on_unix():
    url = _path_to_file_url("/home/user/Writing/Test.odt")
    assert url.startswith("file:///")
    assert url.endswith("/home/user/Writing/Test.odt") or "Writing" in url


def test_normalize_file_url_repairs_legacy_urljoin_form():
    legacy = "file:/home/user/Writing/Test.odt"
    assert _normalize_file_url(legacy) == "file:///home/user/Writing/Test.odt"


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
        for entry in result["files"]:
            assert str(entry["url"]).startswith("file:///")


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
    assert ".xlsx" in NEARBY_FILE_EXTENSIONS
    assert ".docx" in NEARBY_FILE_EXTENSIONS
    assert ".pptx" in NEARBY_FILE_EXTENSIONS
    assert ".txt" in NEARBY_FILE_EXTENSIONS
    assert ".pdf" not in NEARBY_FILE_EXTENSIONS
    assert ".tmp" not in NEARBY_FILE_EXTENSIONS
    assert ".png" not in NEARBY_FILE_EXTENSIONS


def test_nearby_image_extensions_constant():
    assert ".png" in NEARBY_IMAGE_EXTENSIONS
    assert ".jpg" in NEARBY_IMAGE_EXTENSIONS
    assert ".ods" not in NEARBY_IMAGE_EXTENSIONS


def test_list_nearby_files_excludes_images_by_default():
    with tempfile.TemporaryDirectory() as tmp:
        ods = os.path.join(tmp, "Budget.ods")
        png = os.path.join(tmp, "photo.png")
        jpg = os.path.join(tmp, "scan.jpg")
        for path in (ods, png, jpg):
            with open(path, "wb"):
                pass
        model = MagicMock()
        ctx = MagicMock()
        with patch("plugin.doc.document_research.get_document_path", return_value=None):
            with patch("plugin.doc.document_research._collect_open_file_urls", return_value={}):
                with patch("plugin.doc.document_research.resolve_listing_directory", return_value=tmp):
                    result = list_nearby_files(ctx, model)
        assert result["status"] == "ok"
        names = [f["name"] for f in result["files"]]
        assert names == ["Budget.ods"]
        assert "photo.png" not in names
        assert "scan.jpg" not in names


def test_list_nearby_files_file_kind_images():
    with tempfile.TemporaryDirectory() as tmp:
        ods = os.path.join(tmp, "Budget.ods")
        png = os.path.join(tmp, "photo.png")
        for path in (ods, png):
            with open(path, "wb"):
                pass
        model = MagicMock()
        ctx = MagicMock()
        with patch("plugin.doc.document_research.get_document_path", return_value=None):
            with patch("plugin.doc.document_research._collect_open_file_urls", return_value={}):
                with patch("plugin.doc.document_research.resolve_listing_directory", return_value=tmp):
                    result = list_nearby_files(ctx, model, file_kind="images")
        assert result["status"] == "ok"
        names = [f["name"] for f in result["files"]]
        assert names == ["photo.png"]
        assert result["files"][0]["doc_type_guess"] == "image"
        assert "Budget.ods" not in names


def test_list_nearby_files_file_kind_documents_explicit():
    with tempfile.TemporaryDirectory() as tmp:
        ods = os.path.join(tmp, "Budget.ods")
        png = os.path.join(tmp, "photo.png")
        for path in (ods, png):
            with open(path, "wb"):
                pass
        model = MagicMock()
        ctx = MagicMock()
        with patch("plugin.doc.document_research.get_document_path", return_value=None):
            with patch("plugin.doc.document_research._collect_open_file_urls", return_value={}):
                with patch("plugin.doc.document_research.resolve_listing_directory", return_value=tmp):
                    result = list_nearby_files(ctx, model, file_kind="documents")
        assert result["status"] == "ok"
        assert [f["name"] for f in result["files"]] == ["Budget.ods"]


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

# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
"""Unit tests for plugin.doc.document_research."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from unittest.mock import MagicMock, patch


from plugin.doc.document_research import (
    NEARBY_FILE_EXTENSIONS,
    NEARBY_IMAGE_EXTENSIONS,
    _collect_open_file_urls,
    _system_path_from_url,
    close_document_research_document,
    get_open_documents,
    guess_doc_type_from_path,
    get_document_directory,
    get_work_directory,
    list_nearby_files,
    open_document_for_read,
    resolve_listing_directory,
    resolve_path_or_name,
)
from plugin.doc.text_helpers import normalize_file_url
from plugin.framework.url_utils import path_to_file_url


def test_guess_doc_type_from_path():
    assert guess_doc_type_from_path("/tmp/Budget_2026.ods") == "calc"
    assert guess_doc_type_from_path("/tmp/Budget_2026.xlsx") == "calc"
    assert guess_doc_type_from_path("/tmp/Report.odt") == "writer"
    assert guess_doc_type_from_path("/tmp/Report.docx") == "writer"
    assert guess_doc_type_from_path("/tmp/Slides.odp") == "draw"
    assert guess_doc_type_from_path("/tmp/Slides.pptx") == "draw"
    assert guess_doc_type_from_path("/tmp/photo.png") == "image"
    assert guess_doc_type_from_path("/tmp/readme.txt") == "writer"
    assert guess_doc_type_from_path("/tmp/notes.md") == "writer"


def test_path_to_file_url_uses_three_slashes_on_unix():
    url = path_to_file_url("/home/user/Writing/Test.odt")
    assert url.startswith("file:///")
    assert url.endswith("/home/user/Writing/Test.odt") or "Writing" in url


def test_normalize_file_url_repairs_legacy_urljoin_form():
    legacy = "file:/home/user/Writing/Test.odt"
    assert normalize_file_url(legacy) == "file:///home/user/Writing/Test.odt"


def test_system_path_from_url_uses_shared_file_url_repair():
    with patch(
        "plugin.doc.document_research.uno.fileUrlToSystemPath",
        return_value="/home/user/Writing/Test.odt",
    ), patch("plugin.doc.document_research._normalize_path", side_effect=lambda path: path):
        assert _system_path_from_url("file:/home/user/Writing/Test.odt") == "/home/user/Writing/Test.odt"
        assert _system_path_from_url("") is None
        assert _system_path_from_url("private:factory/swriter") is None


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
        ctx.getValueByName.side_effect = lambda name: path_settings if name == "/singletons/com.sun.star.util.thePathSettings" else None
        assert get_work_directory(ctx) == os.path.normpath(tmp)
        ctx.ServiceManager.createInstanceWithContext.assert_not_called()


def test_get_work_directory_substitutes_variables():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = MagicMock()
        path_settings = MagicMock()
        path_settings.getPropertyValue.return_value = "$(home)/Documents"
        ctx.getValueByName.side_effect = lambda name: {
            "/singletons/com.sun.star.util.thePathSettings": path_settings,
            "/singletons/com.sun.star.util.thePathSubstitution": MagicMock(
                substituteVariables=lambda text, _force: tmp if "$(home)" in text else text
            ),
        }.get(name)
        assert get_work_directory(ctx) == os.path.normpath(tmp)


def test_get_work_directory_file_url():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = MagicMock()
        path_settings = MagicMock()
        path_settings.getPropertyValue.return_value = path_to_file_url(tmp)
        ctx.getValueByName.side_effect = lambda name: path_settings if name == "/singletons/com.sun.star.util.thePathSettings" else None
        norm = os.path.normpath(tmp)
        with patch("plugin.doc.document_research._system_path_from_url", return_value=norm):
            assert get_work_directory(ctx) == norm


def test_get_work_directory_uses_singleton_before_create_instance():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = MagicMock()
        path_settings = MagicMock()
        path_settings.getPropertyValue.return_value = tmp
        ctx.getValueByName.side_effect = lambda name: path_settings if name == "/singletons/com.sun.star.util.thePathSettings" else None
        assert get_work_directory(ctx) == os.path.normpath(tmp)
        ctx.ServiceManager.createInstanceWithContext.assert_not_called()


def test_get_work_directory_falls_back_to_create_instance():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = MagicMock()
        path_settings = MagicMock()
        path_settings.getPropertyValue.return_value = tmp
        ctx.getValueByName.return_value = None
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
    assert ".md" in NEARBY_FILE_EXTENSIONS
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


def test_collect_open_file_urls_skips_broken_desktop_component():
    """GHA 34593327841: leftover paste Writer must not abort the nearby walk."""
    with tempfile.TemporaryDirectory() as tmp:
        budget = os.path.join(tmp, "Budget_2026.ods")
        with open(budget, "wb"):
            pass
        budget_url = path_to_file_url(budget)
        budget_norm = os.path.normpath(os.path.abspath(budget))

        broken = MagicMock(name="leftover_paste_writer")
        broken.getController.side_effect = RuntimeError(
            "Couldn't convert <traceback object> to a UNO type"
        )

        good = MagicMock(name="saved_calc")
        good.getController.return_value = None
        good.getURL.return_value = budget_url

        class _Enum:
            def __init__(self, items):
                self._items = list(items)

            def hasMoreElements(self):
                return bool(self._items)

            def nextElement(self):
                return self._items.pop(0)

        desktop = MagicMock()
        desktop.getComponents.return_value.createEnumeration.return_value = _Enum(
            [broken, good]
        )
        with (
            patch("plugin.framework.uno_context.get_desktop", return_value=desktop),
            patch(
                "plugin.doc.document_research._system_path_from_url",
                side_effect=lambda url: budget_norm if url == budget_url else None,
            ),
            patch("plugin.framework.thread_guard.guard_uno", side_effect=lambda obj: obj),
        ):
            out = _collect_open_file_urls(
                object(), exclude_path=None, extensions=NEARBY_FILE_EXTENSIONS
            )
        assert out == {budget_norm: budget_url}


def test_collect_open_file_urls_skips_geturl_failure():
    """Same leftover family: getURL raise must not abort later components."""
    with tempfile.TemporaryDirectory() as tmp:
        budget = os.path.join(tmp, "Budget_2026.ods")
        with open(budget, "wb"):
            pass
        budget_url = path_to_file_url(budget)
        budget_norm = os.path.normpath(os.path.abspath(budget))

        broken = MagicMock(name="leftover_no_url")
        broken.getController.return_value = None
        broken.getURL.side_effect = RuntimeError(
            "Couldn't convert <traceback object> to a UNO type"
        )

        good = MagicMock(name="saved_calc")
        good.getController.return_value = None
        good.getURL.return_value = budget_url

        class _Enum:
            def __init__(self, items):
                self._items = list(items)

            def hasMoreElements(self):
                return bool(self._items)

            def nextElement(self):
                return self._items.pop(0)

        desktop = MagicMock()
        desktop.getComponents.return_value.createEnumeration.return_value = _Enum(
            [broken, good]
        )
        with (
            patch("plugin.framework.uno_context.get_desktop", return_value=desktop),
            patch(
                "plugin.doc.document_research._system_path_from_url",
                side_effect=lambda url: budget_norm if url == budget_url else None,
            ),
            patch("plugin.framework.thread_guard.guard_uno", side_effect=lambda obj: obj),
        ):
            out = _collect_open_file_urls(
                object(), exclude_path=None, extensions=NEARBY_FILE_EXTENSIONS
            )
        assert out == {budget_norm: budget_url}


def test_close_document_research_document_skips_reused_open():
    model = MagicMock()
    close_document_research_document(model, opened_for_document_research=False)
    model.close.assert_not_called()


def test_close_document_research_document_closes_temporary_open():
    model = MagicMock()
    close_document_research_document(model, opened_for_document_research=True)
    model.close.assert_called_once_with(True)


def test_resolve_path_or_name_accepts_file_url_without_listing_filter():
    """file:/// URLs from list_nearby_files / list_open_documents are absolute targets."""
    with tempfile.TemporaryDirectory() as tmp:
        budget = os.path.join(tmp, "Budget.ods")
        with open(budget, "wb"):
            pass
        canonical = path_to_file_url(budget)
        legacy = "file:" + canonical[len("file://") :]
        with (
            patch("plugin.doc.document_research.uno.fileUrlToSystemPath", return_value=budget) as mock_conv,
            patch("plugin.doc.document_research.list_nearby_files") as mock_list,
        ):
            path, url = resolve_path_or_name(MagicMock(), MagicMock(), canonical)
            legacy_path, legacy_url = resolve_path_or_name(MagicMock(), MagicMock(), legacy)
        mock_list.assert_not_called()
        expected = os.path.normpath(os.path.abspath(budget))
        assert path == expected
        assert legacy_path == expected
        assert url == path_to_file_url(expected)
        assert legacy_url == url
        assert mock_conv.call_args_list[1].args[0] == canonical


def test_resolve_path_or_name_missing_file_url_is_not_a_listing_filter():
    missing = "file:///no/such/Budget.ods"
    with (
        patch("plugin.doc.document_research.uno.fileUrlToSystemPath", return_value="/no/such/Budget.ods"),
        patch("plugin.doc.document_research.list_nearby_files") as mock_list,
    ):
        path, err = resolve_path_or_name(MagicMock(), MagicMock(), missing)
    assert path is None
    assert err == f"No file matching {missing!r}"
    mock_list.assert_not_called()


def test_resolve_path_or_name_basename_still_uses_listing():
    with tempfile.TemporaryDirectory() as tmp:
        budget = os.path.join(tmp, "Budget.ods")
        notes = os.path.join(tmp, "Notes.odt")
        for sibling in (budget, notes):
            with open(sibling, "wb"):
                pass
        model = MagicMock()
        with (
            patch("plugin.doc.document_research.get_document_path", return_value=None),
            patch("plugin.doc.document_research._collect_open_file_urls", return_value={}),
            patch("plugin.doc.document_research.resolve_listing_directory", return_value=tmp),
        ):
            path, url = resolve_path_or_name(MagicMock(), model, "Budget.ods")
        assert path == os.path.normpath(budget)
        assert url == path_to_file_url(path)


@patch("plugin.doc.document_research.resolve_document_by_url", return_value=(MagicMock(), "calc"))
@patch("plugin.doc.document_research.os.path.isfile", return_value=True)
def test_open_document_for_read_reuses_existing_without_close_flag(mock_isfile, mock_resolve):
    model, doc_type, err, opened_for_document_research = open_document_for_read(MagicMock(), "/tmp/Budget.ods")
    assert err is None
    assert doc_type == "calc"
    assert opened_for_document_research is False
    mock_resolve.assert_called_once()


def test_hidden_readonly_load_args_named_on_windows(monkeypatch):
    """GHA 34636251918: leftover Hidden _default file load raised system bitmap."""
    import plugin.doc.document_research as dr

    monkeypatch.setattr(dr.sys, "platform", "win32")
    assert dr._hidden_readonly_load_args() == ("_wa_doc_research", 8 | 55)
    monkeypatch.setattr(dr.sys, "platform", "linux")
    assert dr._hidden_readonly_load_args() == ("_default", 0)


@patch("plugin.doc.document_research.get_document_type")
@patch("plugin.framework.uno_context.get_desktop")
@patch("plugin.doc.document_research.resolve_document_by_url", return_value=(None, None))
@patch("plugin.doc.document_research.os.path.isfile", return_value=True)
def test_open_document_for_read_sets_close_flag_on_new_load(mock_isfile, mock_resolve, mock_desktop, mock_dtype):
    from plugin.doc.doc_type import DocumentType

    opened_model = MagicMock()
    mock_desktop.return_value.loadComponentFromURL.return_value = opened_model
    mock_dtype.return_value = DocumentType.CALC
    model, doc_type, err, opened_for_document_research = open_document_for_read(MagicMock(), "/tmp/Budget.ods")
    assert err is None
    assert doc_type == "calc"
    assert model is opened_model
    assert opened_for_document_research is True
    args = mock_desktop.return_value.loadComponentFromURL.call_args.args
    target, flags = (
        ("_wa_doc_research", 8 | 55) if sys.platform == "win32" else ("_default", 0)
    )
    assert args[1] == target
    assert args[2] == flags
    opened_model.close.assert_not_called()


@patch("plugin.doc.document_research.get_document_type")
@patch("plugin.framework.uno_context.get_desktop")
@patch("plugin.doc.document_research.resolve_document_by_url", return_value=(None, None))
@patch("plugin.doc.document_research.os.path.isfile", return_value=True)
def test_open_document_for_read_closes_unsupported_type(mock_isfile, mock_resolve, mock_desktop, mock_dtype):
    """Unknown types must not leave the hidden component loaded."""
    from plugin.doc.doc_type import DocumentType

    opened_model = MagicMock()
    mock_desktop.return_value.loadComponentFromURL.return_value = opened_model
    mock_dtype.return_value = DocumentType.UNKNOWN
    for attempt in range(2):
        model, doc_type, err, opened_for_document_research = open_document_for_read(MagicMock(), "/tmp/photo.png")
        assert model is None
        assert doc_type is None
        assert err is not None and "Unsupported document type" in err
        assert opened_for_document_research is False
        assert opened_model.close.call_count == attempt + 1
    assert opened_model.close.call_count == 2
    opened_model.close.assert_called_with(True)


@patch("plugin.doc.document_research.get_document_type")
@patch("plugin.framework.uno_context.get_desktop")
@patch("plugin.doc.document_research.resolve_document_by_url", return_value=(None, None))
@patch("plugin.doc.document_research.os.path.isfile", return_value=True)
def test_open_document_for_read_closes_when_post_load_raises(mock_isfile, mock_resolve, mock_desktop, mock_dtype):
    """An exception after loadComponentFromURL must not abandon the component."""
    opened_model = MagicMock()
    mock_desktop.return_value.loadComponentFromURL.return_value = opened_model
    mock_dtype.side_effect = RuntimeError("type probe failed")
    model, doc_type, err, opened_for_document_research = open_document_for_read(MagicMock(), "/tmp/Budget.ods")
    assert model is None
    assert doc_type is None
    assert err is not None and "type probe failed" in err
    assert opened_for_document_research is False
    opened_model.close.assert_called_once_with(True)


def test_nearby_uno_env_does_not_open_second_scalc_factory():
    """GHA 34633295036: leftover unique scalc factory failed then hung."""
    path = os.path.join(os.path.dirname(__file__), "test_document_research_uno.py")
    with open(path, encoding="utf-8") as handle:
        src = handle.read()
    assert "create_native_doc" not in src
    assert "store budget via active" in src
    # GHA 34639913692: Hidden load of the storeAsURL path still raised
    # system bitmap after ``_wa_doc_research``. Windows opens a copy.
    assert "Budget_read.ods" in src
    assert "shutil.copy2" in src
    # GHA 34655847157: first Hidden copy bitmap-failed; next hung 30s.
    assert "note_windows_hidden_open_bitmap" in src
    assert "skip_windows_hidden_open_after_bitmap" in src

def test_is_same_document_uses_uid_not_identity():
    from plugin.doc.document_research import _is_same_document

    class Doc:
        def __init__(self, uid, url=""):
            self.RuntimeUID = uid
            self.URL = url

    # Distinct proxy-like objects, same uid -> same document (object identity was always False).
    assert _is_same_document(Doc("u1"), Doc("u1")) is True
    assert _is_same_document(Doc("u1"), Doc("u2")) is False
    assert _is_same_document(None, Doc("u1")) is False
    assert _is_same_document(Doc("u1"), None) is False
    # No uid on either side -> fall back to URL equality (empty URL never matches).
    a, b = Doc(None, "file:///x.odt"), Doc(None, "file:///x.odt")
    assert _is_same_document(a, b) is True
    assert _is_same_document(Doc(None, ""), Doc(None, "")) is False


class _DisposedException(Exception):
    pass


def test_get_open_documents_disposed_desktop_is_not_an_empty_list():
    """A dead desktop enumeration must not look like no documents are open."""
    from plugin.framework.uno_listeners import ListenerBoundary

    desktop = MagicMock()
    enum = MagicMock()
    enum.hasMoreElements.side_effect = _DisposedException("enum dead")
    desktop.getComponents.return_value.createEnumeration.return_value = enum
    docs = None
    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop):
        try:
            try:
                docs = get_open_documents(MagicMock())
            except Exception as exc:
                raise AssertionError("generic handler swallowed a disposed desktop") from exc
        except ListenerBoundary as boundary:
            assert boundary.kind == "disposed"
            return
    raise AssertionError(f"disposed desktop reported as an empty document list: {docs!r}")


def test_get_open_documents_disposed_component_access_is_not_empty():
    from plugin.framework.uno_listeners import ListenerBoundary

    desktop = MagicMock()
    desktop.getComponents.side_effect = _DisposedException("desktop gone")
    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop):
        try:
            try:
                get_open_documents(MagicMock())
            except Exception as exc:
                raise AssertionError("generic handler swallowed a disposed desktop") from exc
        except ListenerBoundary as boundary:
            assert boundary.kind == "disposed"
            return
    raise AssertionError("disposed desktop did not leave get_open_documents")


def test_get_open_documents_empty_desktop_is_not_disposed():
    from plugin.framework.uno_listeners import ListenerBoundary

    desktop = MagicMock()
    desktop.getComponents.return_value = None
    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop):
        try:
            docs = get_open_documents(MagicMock())
        except ListenerBoundary as boundary:
            raise AssertionError(f"empty document list reported as {boundary.kind}") from boundary
    assert docs == []


def test_get_open_documents_off_thread_is_not_an_empty_list():
    """A caller's except Exception must not hide the main-thread check."""
    import threading

    from plugin.framework import thread_guard as tg
    from plugin.framework.uno_listeners import ListenerBoundary
    from tests.harness.strip_bundle import skip_if_release_build

    skip_if_release_build("release bundles stub the main-thread guard")
    was = tg.GUARD_ON
    tg.GUARD_ON = True
    previous_testing = os.environ.get("WRITERAGENT_TESTING")
    os.environ["WRITERAGENT_TESTING"] = "1"
    holder: dict[str, BaseException] = {}

    def _call() -> None:
        try:
            try:
                get_open_documents(MagicMock())
            except Exception as exc:
                holder["swallowed"] = exc
        except BaseException as exc:
            holder["exc"] = exc

    try:
        worker = threading.Thread(target=_call, name="bg-open-docs")
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert "swallowed" not in holder
        exc = holder.get("exc")
        assert isinstance(exc, ListenerBoundary)
        assert exc.kind == "thread"
    finally:
        tg.GUARD_ON = was
        if previous_testing is None:
            os.environ.pop("WRITERAGENT_TESTING", None)
        else:
            os.environ["WRITERAGENT_TESTING"] = previous_testing


def test_get_open_documents_runtime_error_is_not_disposal_or_empty():
    from plugin.framework.uno_listeners import ListenerBoundary

    desktop = MagicMock()
    enum = MagicMock()
    enum.hasMoreElements.side_effect = RuntimeError("not disposed")
    desktop.getComponents.return_value.createEnumeration.return_value = enum
    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop):
        try:
            get_open_documents(MagicMock())
        except ListenerBoundary as boundary:
            raise AssertionError(f"runtime error reported as {boundary.kind}") from boundary
        except RuntimeError as exc:
            assert "not disposed" in str(exc)
            return
    raise AssertionError("runtime error was reported as an empty document list")

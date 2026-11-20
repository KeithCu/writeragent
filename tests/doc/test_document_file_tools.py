import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from plugin.doc.document_file_tools import (
    DocumentExport,
    DocumentOpen,
    DocumentSave,
    export_filter,
)
from plugin.doc.document_research import resolve_file_target
from plugin.framework.doc_type import DocumentType
from plugin.framework.tool import ToolRegistry


def _make_model(url: str = "file:///tmp/test.odt", uid: str = "uid-1", *, modified: bool = False, read_only: bool = False):
    model = MagicMock()
    model.getURL.return_value = url
    model.hasLocation.return_value = bool(url)
    model.isModified.return_value = modified
    model.isReadonly.return_value = read_only
    model.RuntimeUID = uid
    return model


def _make_ctx(doc=None):
    ctx = MagicMock()
    ctx.ctx = MagicMock()
    ctx.doc = doc
    return ctx


def test_document_file_tools_registry_filtering():
    registry = ToolRegistry(MagicMock())
    open_tool = DocumentOpen()
    save_tool = DocumentSave()
    export_tool = DocumentExport()
    for tool in (open_tool, save_tool, export_tool):
        registry.register(tool)

    default_tools = registry.get_tools()
    assert open_tool not in default_tools
    assert save_tool not in default_tools
    assert export_tool not in default_tools

    mcp_tools = registry.get_tools(exclude_tiers=frozenset({"specialized", "specialized_control"}))
    assert open_tool in mcp_tools
    assert save_tool in mcp_tools
    assert export_tool in mcp_tools

    assert export_tool.requires_document_lock() is True


def test_export_filter():
    assert export_filter("writer", "/x/Modèle.DOCX", save=True) == "MS Word 2007 XML"
    assert export_filter("calc", "/x/a.pdf", save=False) == "calc_pdf_Export"

    err_pdf = export_filter("writer", "/x/a.pdf", save=True)
    assert isinstance(err_pdf, tuple)
    assert err_pdf[0] == "EXPORT_ONLY_FORMAT" and "document_export" in err_pdf[1]

    err_xyz = export_filter("writer", "/x/a.xyz", save=False)
    assert isinstance(err_xyz, tuple)
    assert err_xyz[0] == "UNKNOWN_FORMAT" and ".odt" in err_xyz[1] and ".docx" in err_xyz[1]

    err_unknown = export_filter("unknown", "/x/a.odt", save=True)
    assert isinstance(err_unknown, tuple)
    assert err_unknown[0] == "UNSUPPORTED_DOCUMENT"

    assert export_filter("impress", "/x/a.pptx", save=True) == "Impress MS PowerPoint 2007 XML"
    draw_err = export_filter("draw", "/x/a.pptx", save=True)
    assert isinstance(draw_err, tuple)
    assert draw_err[0] == "UNKNOWN_FORMAT"


def test_resolve_file_target(tmp_path):
    assert resolve_file_target("relative/doc.odt") is None
    assert resolve_file_target("http://example.com/doc.odt") is None

    target = tmp_path / "wa probe" / "Modèle é.odt"
    resolved = resolve_file_target(str(target))
    assert resolved is not None
    path, url = resolved
    assert path == str(target)
    assert url == target.as_uri()
    assert url.startswith("file:///")
    assert "%20" in url
    assert "%C3%A9" in url

    rooted = resolve_file_target("/wa-rooted/x.odt")
    assert rooted is not None
    assert rooted[0] == os.path.normpath(os.path.abspath("/wa-rooted/x.odt"))

    url_target = tmp_path / "from url.odt"
    assert resolve_file_target(url_target.as_uri()) == (
        str(url_target),
        url_target.as_uri(),
    )


def test_document_open_missing_file(tmp_path):
    missing = tmp_path / "missing.odt"
    with patch("plugin.doc.document_file_tools.get_desktop") as mock_desktop:
        res = DocumentOpen().execute(_make_ctx(), path=str(missing))
    assert res["status"] == "error"
    assert res["code"] == "FILE_NOT_FOUND"
    mock_desktop.assert_not_called()


def test_document_open_fresh_file(tmp_path):
    target = tmp_path / "hello.odt"
    target.write_text("x", encoding="utf-8")
    model = _make_model(url=target.as_uri(), uid="uid-new")
    desktop = MagicMock()
    desktop.loadComponentFromURL.return_value = model

    with patch("plugin.doc.document_file_tools.resolve_document_by_url", return_value=(None, None)), \
         patch("plugin.doc.document_file_tools.get_desktop", return_value=desktop), \
         patch("plugin.doc.document_research.system_path_from_url", return_value=str(target)):
        res = DocumentOpen().execute(_make_ctx(), path=str(target))

    assert res["status"] == "ok"
    assert res["already_open"] is False
    assert res["document"]["uid"] == "uid-new"
    assert desktop.loadComponentFromURL.call_args.args == (target.as_uri(), "_default", 0, ())


def test_document_open_as_template(tmp_path):
    target = tmp_path / "tpl.ott"
    target.write_text("x", encoding="utf-8")
    model = _make_model(url="", uid="uid-tpl")
    desktop = MagicMock()
    desktop.loadComponentFromURL.return_value = model
    prop = SimpleNamespace(Name="AsTemplate", Value=True)

    with patch("plugin.doc.document_file_tools.resolve_document_by_url") as mock_resolve, \
         patch("plugin.doc.document_file_tools.get_desktop", return_value=desktop), \
         patch("plugin.doc.document_file_tools.create_property_value", return_value=prop) as mock_cpv, \
         patch("plugin.doc.document_research.get_document_type", return_value=DocumentType.WRITER):
        res = DocumentOpen().execute(_make_ctx(), path=str(target), as_template=True)

    mock_resolve.assert_not_called()
    mock_cpv.assert_called_once_with("AsTemplate", True)
    props_arg = desktop.loadComponentFromURL.call_args.args[3]
    assert len(props_arg) == 1 and props_arg[0].Name == "AsTemplate"
    assert res["status"] == "ok"
    assert res["already_open"] is False


def test_document_open_already_open(tmp_path):
    target = tmp_path / "open.odt"
    target.write_text("x", encoding="utf-8")
    existing = _make_model(url=target.as_uri(), uid="uid-open")

    with patch("plugin.doc.document_file_tools.resolve_document_by_url", return_value=(existing, "writer")), \
         patch("plugin.doc.document_file_tools.get_desktop") as mock_desktop, \
         patch("plugin.doc.document_research.system_path_from_url", return_value=str(target)):
        res = DocumentOpen().execute(_make_ctx(), path=str(target))

    mock_desktop.assert_not_called()
    assert res["status"] == "ok"
    assert res["already_open"] is True
    assert res["document"]["uid"] == "uid-open"


def test_document_open_load_returns_none(tmp_path):
    target = tmp_path / "bad.odt"
    target.write_text("x", encoding="utf-8")
    desktop = MagicMock()
    desktop.loadComponentFromURL.return_value = None

    with patch("plugin.doc.document_file_tools.resolve_document_by_url", return_value=(None, None)), \
         patch("plugin.doc.document_file_tools.get_desktop", return_value=desktop):
        res = DocumentOpen().execute(_make_ctx(), path=str(target))

    assert res["status"] == "error"
    assert res["code"] == "OPEN_FAILED"


def test_document_save_in_place_and_errors():
    untitled = _make_model(url="")
    res_untitled = DocumentSave().execute(_make_ctx(untitled))
    assert res_untitled["code"] == "NO_LOCATION"
    untitled.store.assert_not_called()

    ro_doc = _make_model(read_only=True)
    res_ro = DocumentSave().execute(_make_ctx(ro_doc))
    assert res_ro["code"] == "READ_ONLY"
    ro_doc.store.assert_not_called()

    saved_doc = _make_model(url="file:///tmp/ok.odt", uid="uid-saved")
    with patch("plugin.doc.document_research.system_path_from_url", return_value="/tmp/ok.odt"):
        res_ok = DocumentSave().execute(_make_ctx(saved_doc))
        res_empty = DocumentSave().execute(_make_ctx(saved_doc), path="")
    assert res_ok["status"] == "ok"
    assert res_empty["status"] == "ok"
    assert saved_doc.store.call_count == 2


def test_document_save_as_formats(tmp_path):
    doc = _make_model(url="file:///tmp/src.odt", uid="uid-1")
    with patch("plugin.doc.document_file_tools.get_document_type", return_value=DocumentType.WRITER):
        res_pdf = DocumentSave().execute(_make_ctx(doc), path=str(tmp_path / "out.pdf"))
        assert res_pdf["code"] == "EXPORT_ONLY_FORMAT"

        docx_path = tmp_path / "out.docx"
        prop = SimpleNamespace(Name="FilterName", Value="MS Word 2007 XML")
        with patch("plugin.doc.document_file_tools.create_property_value", return_value=prop) as mock_cpv, \
             patch("plugin.doc.document_research.system_path_from_url", return_value=str(docx_path)):
            res_docx = DocumentSave().execute(_make_ctx(doc), path=str(docx_path))

    assert res_docx["status"] == "ok"
    mock_cpv.assert_called_once_with("FilterName", "MS Word 2007 XML")
    doc.storeAsURL.assert_called_once_with(docx_path.as_uri(), (prop,))


def test_document_export(tmp_path):
    doc = _make_model(url="file:///tmp/src.odt", uid="uid-1")
    with patch("plugin.doc.document_file_tools.get_document_type", return_value=DocumentType.WRITER):
        res_bad = DocumentExport().execute(_make_ctx(doc), path=str(tmp_path / "out.xyz"))
        assert res_bad["code"] == "UNKNOWN_FORMAT"

        pdf_path = tmp_path / "out.pdf"
        prop = SimpleNamespace(Name="FilterName", Value="writer_pdf_Export")
        with patch("plugin.doc.document_file_tools.create_property_value", return_value=prop) as mock_cpv, \
             patch("plugin.doc.document_research.system_path_from_url", return_value="/tmp/src.odt"):
            res_pdf = DocumentExport().execute(_make_ctx(doc), path=str(pdf_path))

    assert res_pdf["status"] == "ok"
    assert res_pdf["path"] == str(pdf_path)
    mock_cpv.assert_called_once_with("FilterName", "writer_pdf_Export")
    doc.storeToURL.assert_called_once_with(pdf_path.as_uri(), (prop,))


def test_document_save_and_export_missing_folder(tmp_path):
    doc = _make_model(url="file:///tmp/src.odt", uid="uid-1")
    missing_dir = tmp_path / "does-not-exist"
    with patch("plugin.doc.document_file_tools.get_document_type", return_value=DocumentType.WRITER):
        res_save = DocumentSave().execute(_make_ctx(doc), path=str(missing_dir / "out.odt"))
        res_export = DocumentExport().execute(_make_ctx(doc), path=str(missing_dir / "out.pdf"))

    assert res_save["status"] == "error"
    assert res_save["code"] == "DIRECTORY_NOT_FOUND"
    assert str(missing_dir) in res_save["message"]
    doc.storeAsURL.assert_not_called()

    assert res_export["status"] == "error"
    assert res_export["code"] == "DIRECTORY_NOT_FOUND"
    assert str(missing_dir) in res_export["message"]
    doc.storeToURL.assert_not_called()

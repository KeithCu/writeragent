# WriterAgent — unit tests for Impress design enumeration and LO-wall apply
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.draw.designs import (
    ApplyDesign,
    ListDesigns,
    SetPresentationDesign,
    _blank_master_signal,
    _split_pathsettings_value,
    current_doc_apply_error,
    enumerate_impress_designs,
    inherit_master_from_neighbor,
    resolve_design,
)
from plugin.draw.pages import AddSlide
from plugin.framework.url_utils import path_to_file_url


def test_split_pathsettings_space_separated_file_urls():
    raw = "file:///usr/share/t1 file:///usr/share/t2"
    assert _split_pathsettings_value(raw) == ["file:///usr/share/t1", "file:///usr/share/t2"]


def test_split_pathsettings_semicolon():
    assert _split_pathsettings_value("/a/templates;/b/templates") == ["/a/templates", "/b/templates"]


def test_enumerate_designs_from_mocked_pathsettings(tmp_path):
    presnt = tmp_path / "common" / "presnt"
    presnt.mkdir(parents=True)
    otp = presnt / "Metropolis.otp"
    otp.write_bytes(b"PK")
    (presnt / "~$lock.otp").write_bytes(b"x")
    (tmp_path / "common" / "readme.txt").write_text("no")

    settings = MagicMock()
    settings.getPropertySetInfo.return_value = None
    settings.getPropertyValue.side_effect = lambda name: path_to_file_url(str(tmp_path / "common")) if name == "Template" else ""

    ctx = MagicMock()
    ctx.getValueByName.side_effect = lambda name: settings if name == "/singletons/com.sun.star.util.thePathSettings" else None

    with patch("plugin.draw.designs._resolve_lo_directory_path", return_value=str(tmp_path / "common")):
        designs = enumerate_impress_designs(ctx)
    assert len(designs) == 1
    assert designs[0]["id"] == "metropolis"
    assert designs[0]["name"] == "Metropolis"
    assert designs[0]["path"] == str(otp)
    assert designs[0]["url"].startswith("file:")
    assert "/usr/lib/libreoffice" not in designs[0]["path"]


def test_resolve_design_by_id_and_name(tmp_path):
    d = {
        "id": "metropolis",
        "name": "Metropolis",
        "path": str(tmp_path / "Metropolis.otp"),
        "url": "file:///tmp/Metropolis.otp",
    }
    with patch("plugin.draw.designs.enumerate_impress_designs", return_value=[d]):
        assert resolve_design(None, "Metropolis")["id"] == "metropolis"
        assert resolve_design(None, "metropolis")["name"] == "Metropolis"
        assert resolve_design(None, "missing") is None


def test_apply_design_current_doc_is_lo_wall():
    ctx = MagicMock()
    ctx.doc.supportsService.side_effect = lambda s: s == "com.sun.star.presentation.PresentationDocument"
    out = ApplyDesign().execute(ctx, design="Metropolis", new_document=False)
    assert out["status"] == "error"
    assert out["code"] == "LO_WALL"
    assert "current" in out["message"].lower() or "loadStylesFromURL" in out["message"]
    details = out.get("details") or {}
    assert details.get("reason") == "current_doc_apply_unsupported"


def test_set_presentation_design_draw_not_impress():
    ctx = MagicMock()
    ctx.doc.supportsService.side_effect = lambda s: s == "com.sun.star.drawing.DrawingDocument"
    out = SetPresentationDesign().execute(ctx, design="Metropolis")
    assert out["status"] == "error"
    assert out["code"] == "UNSUPPORTED_DOC_TYPE"
    assert "not a Draw" in out["message"] or "Impress" in out["message"]


def test_apply_design_draw_not_impress():
    ctx = MagicMock()
    ctx.doc.supportsService.side_effect = lambda s: s == "com.sun.star.drawing.DrawingDocument"
    out = ApplyDesign().execute(ctx, design="Metropolis", new_document=True)
    assert out["status"] == "error"
    assert out["code"] == "UNSUPPORTED_DOC_TYPE"


def test_list_designs_tool_returns_count():
    ctx = MagicMock()
    with patch("plugin.draw.designs.enumerate_impress_designs", return_value=[{"id": "metropolis"}]):
        out = ListDesigns().execute(ctx)
    assert out["status"] == "ok"
    assert out["count"] == 1


def test_blank_master_signal():
    assert _blank_master_signal([]) is True
    assert _blank_master_signal([{"name": "Default", "shape_count": 1}]) is True
    assert _blank_master_signal([{"name": "Metropolis", "shape_count": 6}]) is False


def test_inherit_master_from_neighbor_copies_previous():
    prev_master = MagicMock()
    prev_master.Name = "Designed"
    prev = MagicMock()
    prev.MasterPage = prev_master
    new_page = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 2
    pages.getByIndex.side_effect = lambda i: prev if i == 0 else new_page
    name = inherit_master_from_neighbor(pages, new_page, 1)
    assert name == "Designed"
    assert new_page.MasterPage is prev_master


def test_add_slide_reports_inherited_master():
    ctx = MagicMock()
    ctx.doc.supportsService.return_value = True
    page = MagicMock()
    page.Layout = 20
    pages = MagicMock()
    pages.getCount.return_value = 1
    bridge = MagicMock()
    bridge.create_slide.return_value = page
    bridge.get_active_page_index.return_value = 1
    bridge.get_pages.return_value = pages
    with (
        patch("plugin.draw.bridge.DrawBridge", return_value=bridge),
        patch("plugin.draw.designs.inherit_master_from_neighbor", return_value="Metropolis"),
    ):
        out = AddSlide().execute(ctx)
    assert out["status"] == "ok"
    assert out["master"] == "Metropolis"


def test_current_doc_apply_error_payload():
    tool = ApplyDesign()
    err = current_doc_apply_error(tool)
    assert err["code"] == "LO_WALL"
    assert err["status"] == "error"


def test_designs_module_has_no_hardcoded_install_prefix():
    import inspect

    import plugin.draw.designs as designs

    src = inspect.getsource(designs)
    assert "/usr/lib/libreoffice" not in src
    assert "/opt/libreoffice" not in src

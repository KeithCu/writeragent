# WriterAgent — unit tests for Impress design enumeration and LO-wall apply
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import struct
import zipfile
import zlib
from unittest.mock import MagicMock, patch

from plugin.draw.designs import (
    ApplyDesign,
    ListDesigns,
    SetPresentationDesign,
    _blank_master_signal,
    _clone_one_shape,
    _is_safe_style_value,
    _split_pathsettings_value,
    apply_design_to_current_doc,
    enumerate_impress_designs,
    find_imported_master,
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
    assert designs[0]["look"] == ""
    assert "/usr/lib/libreoffice" not in designs[0]["path"]


def _rgb_png(color: tuple[int, int, int], width: int = 8, height: int = 8) -> bytes:
    raw = b""
    for unused_y in range(height):
        raw += b"\x00" + bytes(color) * width
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def test_enumerate_includes_derived_look(tmp_path):
    presnt = tmp_path / "presnt"
    presnt.mkdir()
    otp = presnt / "Navy.otp"
    with zipfile.ZipFile(otp, "w") as zf:
        zf.writestr("Thumbnails/thumbnail.png", _rgb_png((20, 40, 120)))
        zf.writestr("Pictures/mark.svg", b"<svg/>")

    settings = MagicMock()
    settings.getPropertySetInfo.return_value = None
    settings.getPropertyValue.side_effect = lambda name: path_to_file_url(str(presnt)) if name == "Template" else ""
    ctx = MagicMock()
    ctx.getValueByName.side_effect = lambda name: settings if name == "/singletons/com.sun.star.util.thePathSettings" else None

    with patch("plugin.draw.designs._resolve_lo_directory_path", return_value=str(presnt)):
        designs = enumerate_impress_designs(ctx)
    assert len(designs) == 1
    assert designs[0]["look"] == "dark background; blue accents; graphic chrome"


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


def test_apply_design_current_doc_calls_import_path():
    ctx = MagicMock()
    ctx.doc.supportsService.side_effect = lambda s: s == "com.sun.star.presentation.PresentationDocument"
    entry = {"id": "metropolis", "name": "Metropolis", "path": "/tmp/Metropolis.otp"}
    payload = {
        "status": "ok",
        "applied_master": "Metropolis",
        "blank_master": False,
        "slides_updated": 2,
        "applied_master_shape_count": 6,
    }
    with (
        patch("plugin.draw.designs.resolve_design", return_value=entry),
        patch("plugin.draw.designs.apply_design_to_current_doc", return_value=payload) as apply_cur,
    ):
        out = ApplyDesign().execute(ctx, design="Metropolis", new_document=False)
    assert out["status"] == "ok"
    assert out["applied_master"] == "Metropolis"
    assert out["blank_master"] is False
    apply_cur.assert_called_once()


def test_apply_design_current_doc_import_failure():
    ctx = MagicMock()
    ctx.doc.supportsService.side_effect = lambda s: s == "com.sun.star.presentation.PresentationDocument"
    entry = {"id": "metropolis", "name": "Metropolis", "path": "/tmp/Metropolis.otp"}
    with (
        patch("plugin.draw.designs.resolve_design", return_value=entry),
        patch(
            "plugin.draw.designs.apply_design_to_current_doc",
            side_effect=RuntimeError("paste failed"),
        ),
    ):
        out = ApplyDesign().execute(ctx, design="Metropolis", new_document=False)
    assert out["status"] == "error"
    assert out.get("code") != "LO_WALL"
    details = out.get("details") or {}
    assert details.get("reason") == "current_doc_import_failed"
    assert "current" in out["message"].lower() or "failed" in out["message"].lower()


def test_apply_design_description_mentions_current_doc():
    desc = ApplyDesign.description.lower()
    assert "current" in desc or "open" in desc
    assert "lo_wall" not in desc
    assert "new_document" in desc
    assert "does not use" in desc and "clipboard" in desc
    assert "diamode" not in desc
    assert "clone" in desc


def test_apply_design_to_current_doc_uses_clone_not_clipboard():
    dest = MagicMock()
    dest.getDrawPages().getCount.return_value = 2
    src = MagicMock()
    master = MagicMock()
    with (
        patch("plugin.draw.designs.open_design_source_hidden", return_value=src),
        patch("plugin.draw.designs.clone_master_into_doc", return_value=(master, "Metropolis", 6)) as clone,
        patch("plugin.draw.designs._close_hidden_doc") as closer,
        patch("plugin.draw.designs.assign_master_to_all_slides", return_value=2),
        patch(
            "plugin.draw.designs._master_entries",
            return_value=[{"name": "Metropolis", "shape_count": 6}],
        ),
    ):
        out = apply_design_to_current_doc(
            MagicMock(), dest, {"id": "metropolis", "name": "Metropolis"}
        )
    assert out["status"] == "ok"
    assert out["import_method"] == "clone_master"
    assert out["applied_master"] == "Metropolis"
    assert out["applied_master_shape_count"] == 6
    clone.assert_called_once()
    closer.assert_called_once_with(src)


def test_clone_one_shape_sets_geometry_after_add():
    calls: list[str] = []
    dest_doc = MagicMock()
    clone = MagicMock()
    dest_doc.createInstance.return_value = clone
    dest_master = MagicMock()
    dest_master.add.side_effect = lambda _unused: calls.append("add")
    src = MagicMock()
    src.ShapeType = "com.sun.star.drawing.GraphicObjectShape"
    with patch(
        "plugin.draw.designs._copy_uno_prop",
        side_effect=lambda _dest, _src, name: calls.append(name) or True,
    ):
        out = _clone_one_shape(dest_doc, dest_master, src)
    assert out == "com.sun.star.drawing.GraphicObjectShape"
    assert calls[0] == "add"
    assert calls[-2:] == ["Position", "Size"]
    dest_doc.createInstance.assert_called_once_with("com.sun.star.drawing.GraphicObjectShape")


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


def test_list_designs_description_mentions_look():
    desc = ListDesigns.description.lower()
    assert "look" in desc
    assert "appearance" in desc or "mood" in desc or "dark" in desc


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


def test_find_imported_master_prefers_design_name():
    metro = MagicMock()
    metro.Name = "Metropolis"
    metro.getCount.return_value = 6
    default = MagicMock()
    default.Name = "Default"
    default.getCount.return_value = 5
    masters = MagicMock()
    masters.getCount.return_value = 2
    masters.getByIndex.side_effect = lambda i: default if i == 0 else metro
    doc = MagicMock()
    doc.getMasterPages.return_value = masters
    found, name, shapes = find_imported_master(
        doc, {"id": "metropolis", "name": "Metropolis"}, {"Default"}
    )
    assert found is metro
    assert name == "Metropolis"
    assert shapes == 6


def test_safe_style_value_rejects_uno_structs():
    assert _is_safe_style_value(16777215) is True
    assert _is_safe_style_value(33.0) is True
    assert _is_safe_style_value("Liberation Sans") is True
    assert _is_safe_style_value(True) is True
    assert _is_safe_style_value(MagicMock()) is False
    assert _is_safe_style_value({"Color": 1}) is False


def test_clone_one_shape_rejects_non_uno_type():
    src = MagicMock()
    src.ShapeType = "NotAShape"
    try:
        _clone_one_shape(MagicMock(), MagicMock(), src)
    except RuntimeError as exc:
        assert "Unsupported" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_designs_module_has_no_hardcoded_install_prefix():
    import inspect

    import plugin.draw.design_look as design_look
    import plugin.draw.designs as designs

    for mod in (designs, design_look):
        src = inspect.getsource(mod)
        assert "/usr/lib/libreoffice" not in src
        assert "/opt/libreoffice" not in src

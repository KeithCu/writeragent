# WriterAgent — unit tests for Impress design enumeration and LO-wall apply
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path
import struct
import zipfile
import zlib
from unittest.mock import MagicMock, patch

import pytest

from plugin.draw.designs import (
    ApplyDesign,
    ClonedMaster,
    ListDesigns,
    _blank_master_signal,
    _clone_one_shape,
    _split_pathsettings_value,
    apply_design_to_current_doc,
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
    with patch("plugin.draw.designs.enumerate_impress_designs", return_value=[d]) as enum:
        m1 = resolve_design(None, "Metropolis")
        assert m1 is not None and m1["id"] == "metropolis"
        m2 = resolve_design(None, "metropolis")
        assert m2 is not None and m2["name"] == "Metropolis"
        assert resolve_design(None, "missing") is None
    enum.assert_called_with(None, include_look=False)


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
        patch("plugin.draw.designs.enumerate_impress_designs", return_value=[entry]),
        patch("plugin.draw.designs.apply_design_to_current_doc", return_value=payload) as apply_cur,
    ):
        out = ApplyDesign().execute(ctx, design="Metropolis")
    assert out["status"] == "ok"
    assert out["applied_master"] == "Metropolis"
    assert out["blank_master"] is False
    apply_cur.assert_called_once()


def test_apply_design_current_doc_import_failure():
    ctx = MagicMock()
    ctx.doc.supportsService.side_effect = lambda s: s == "com.sun.star.presentation.PresentationDocument"
    entry = {"id": "metropolis", "name": "Metropolis", "path": "/tmp/Metropolis.otp"}
    with (
        patch("plugin.draw.designs.enumerate_impress_designs", return_value=[entry]),
        patch(
            "plugin.draw.designs.apply_design_to_current_doc",
            side_effect=RuntimeError("paste failed"),
        ),
    ):
        out = ApplyDesign().execute(ctx, design="Metropolis")
    assert out["status"] == "error"
    assert out.get("code") != "LO_WALL"
    details = out.get("details") or {}
    assert details.get("reason") == "current_doc_import_failed"
    assert "current" in out["message"].lower() or "failed" in out["message"].lower()


def test_apply_design_description_mentions_current_doc():
    desc = ApplyDesign.description.lower()
    assert "current" in desc or "open" in desc
    assert "lo_wall" not in desc
    assert "new_document" not in desc
    assert "does not use" in desc and "clipboard" in desc
    assert "diamode" not in desc
    assert "clone" in desc
    params = ApplyDesign.parameters or {}
    assert "new_document" not in params.get("properties", {})


def test_apply_design_ignores_legacy_new_document_kwarg():
    """Leftover new_document=true must still restyle the open deck, not spawn one."""
    ctx = MagicMock()
    ctx.doc.supportsService.side_effect = lambda s: s == "com.sun.star.presentation.PresentationDocument"
    entry = {"id": "metropolis", "name": "Metropolis", "path": "/tmp/Metropolis.otp"}
    payload = {"status": "ok", "applied_master": "Metropolis", "import_method": "clone_master"}
    with (
        patch("plugin.draw.designs.enumerate_impress_designs", return_value=[entry]),
        patch("plugin.draw.designs.apply_design_to_current_doc", return_value=payload) as apply_cur,
        patch("plugin.draw.designs.create_presentation_from_design") as create_new,
    ):
        out = ApplyDesign().execute(ctx, design="Metropolis", new_document=True)
    assert out["status"] == "ok"
    apply_cur.assert_called_once()
    create_new.assert_not_called()


def test_apply_design_to_current_doc_uses_clone_not_clipboard():
    dest = MagicMock()
    dest.getDrawPages().getCount.return_value = 2
    src = MagicMock()
    master = MagicMock()
    with (
        patch("plugin.draw.designs.open_design_source_hidden", return_value=src),
        patch("plugin.draw.designs.clone_master_into_doc", return_value=ClonedMaster(master, "Metropolis", 6)) as clone,
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
    assert out[0] == "com.sun.star.drawing.GraphicObjectShape"
    assert calls[0] == "add"
    assert calls[-2:] == ["Position", "Size"]
    dest_doc.createInstance.assert_called_once_with("com.sun.star.drawing.GraphicObjectShape")


def test_apply_design_draw_not_impress():
    ctx = MagicMock()
    ctx.doc.supportsService.side_effect = lambda s: s == "com.sun.star.drawing.DrawingDocument"
    out = ApplyDesign().execute(ctx, design="Metropolis")
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
    assert "apply_design" in desc
    assert "set_presentation_design" not in desc


def test_draw_prompt_steers_apply_design_on_open_deck():
    from plugin.framework.prompts import DEFAULT_DRAW_CHAT_SYSTEM_PROMPT_TEMPLATE

    text = DEFAULT_DRAW_CHAT_SYSTEM_PROMPT_TEMPLATE
    assert "apply_design" in text
    assert "list_designs" in text
    assert "set_presentation_design" not in text
    assert "new_document" not in text


def test_set_presentation_design_is_not_a_tool():
    import plugin.draw.designs as designs

    assert not hasattr(designs, "SetPresentationDesign")
    assert not hasattr(designs, "enable_design_headers_footers")


def test_blank_master_signal():
    assert _blank_master_signal([]) is True
    assert _blank_master_signal([{"name": "Default", "shape_count": 1}]) is True
    assert _blank_master_signal([{"name": "Metropolis", "shape_count": 6}]) is False


def test_inherit_master_from_neighbor_at_zero_skips_the_new_page():
    """insert_at=0 must not copy the new page's own master.

    The off-by-one left the new page at index 1 while the caller still
    passed insert_at=0, so the only adjacent slot was the new page itself.
    """
    designed = MagicMock()
    designed.Name = "Designed"
    neighbor = MagicMock()
    neighbor.MasterPage = designed
    new_page = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 2
    pages.getByIndex.side_effect = lambda i: neighbor if i == 0 else new_page
    name = inherit_master_from_neighbor(pages, new_page, 0)
    assert name == "Designed"
    assert new_page.MasterPage is designed


def test_inherit_master_from_neighbor_at_zero_uses_following_slide():
    """When the new page really occupies index 0, copy the slide now at 1."""
    designed = MagicMock()
    designed.Name = "Designed"
    neighbor = MagicMock()
    neighbor.MasterPage = designed
    new_page = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 2
    pages.getByIndex.side_effect = lambda i: new_page if i == 0 else neighbor
    name = inherit_master_from_neighbor(pages, new_page, 0)
    assert name == "Designed"
    assert new_page.MasterPage is designed


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
    bridge.create_slide.return_value = (page, 1)
    bridge.get_active_page_index.return_value = 1
    bridge.get_pages.return_value = pages
    with (
        patch("plugin.draw.bridge.DrawBridge", return_value=bridge),
        patch("plugin.draw.designs.inherit_master_from_neighbor", return_value="Metropolis"),
    ):
        out = AddSlide().execute(ctx)
    assert out["status"] == "ok"
    assert out["master"] == "Metropolis"


def test_apply_design_partial_assign_is_error():
    dest = MagicMock()
    dest.getDrawPages().getCount.return_value = 2
    src = MagicMock()
    master = MagicMock()
    with (
        patch("plugin.draw.designs.open_design_source_hidden", return_value=src),
        patch("plugin.draw.designs.clone_master_into_doc", return_value=ClonedMaster(master, "Metropolis", 6)),
        patch("plugin.draw.designs._close_hidden_doc"),
        patch("plugin.draw.designs.assign_master_to_all_slides", return_value=1),
        patch("plugin.draw.designs._master_entries", return_value=[]),
    ):
        out = apply_design_to_current_doc(MagicMock(), dest, {"id": "metropolis", "name": "Metropolis"})
    assert out["status"] == "error"
    assert out["code"] == "DESIGN_PARTIAL_APPLY"
    assert out["slides_updated"] == 1
    assert out["slide_count"] == 2
    assert "Rolled back" in out["message"]
    assert out["rolled_back"] is True


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


def test_extract_member_capped_and_safe_paths(tmp_path):
    import zipfile
    from plugin.draw.otp_zip import extract_member

    otp = tmp_path / "test.otp"
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    with zipfile.ZipFile(otp, "w") as zf:
        zf.writestr("overcap.bin", b"x" * (2 * 1024 * 1024 + 10))
        zf.writestr("../..", b"sneaky")
        zf.writestr("..", b"sneaky2")
        zf.writestr("Pictures/c.png", b"normal_pic")

    with zipfile.ZipFile(otp) as zf:
        # Over-cap member returns None
        assert extract_member(zf, "overcap.bin", str(dest_dir)) is None
        # Traversal names do not extract or create files outside dest_dir
        assert extract_member(zf, "../..", str(dest_dir)) is None
        assert extract_member(zf, "..", str(dest_dir)) is None
        # Normal member extracts
        out = extract_member(zf, "Pictures/c.png", str(dest_dir), prefix="test_")
        assert out is not None
        assert Path(out).read_bytes() == b"normal_pic"
        assert Path(out).parent == dest_dir

def test_enumerate_impress_designs_limit():
    import tempfile
    import os
    from unittest.mock import patch
    from plugin.draw.designs import enumerate_impress_designs

    with tempfile.TemporaryDirectory() as td:
        for i in range(1005):
            open(os.path.join(td, f"file{i}.otp"), "w").close()

        class MockCtx:
            pass

        with patch("plugin.draw.designs._iter_template_directories", return_value=[td]):
            designs = enumerate_impress_designs(MockCtx(), include_look=False)
            assert len(designs) == 1000
            names = [d["name"] for d in designs]
            assert names == sorted(names, key=str.lower)


def test_master_entries_reraise_disposed():
    from plugin.draw.designs import _master_entries

    class DisposedException(Exception):
        pass

    doc = MagicMock()
    doc.getMasterPages.side_effect = DisposedException("closed")

    with pytest.raises(DisposedException):
        _master_entries(doc)

    doc.getMasterPages.side_effect = RuntimeError("failed")
    assert _master_entries(doc) == []


def test_enumerate_skip_look_does_not_open_zip(tmp_path):
    presnt = tmp_path / "presnt"
    presnt.mkdir()
    (presnt / "Metropolis.otp").write_bytes(b"PK")
    settings = MagicMock()
    settings.getPropertySetInfo.return_value = None
    settings.getPropertyValue.side_effect = lambda name: path_to_file_url(str(presnt)) if name == "Template" else ""
    ctx = MagicMock()
    ctx.getValueByName.side_effect = lambda name: settings if name == "/singletons/com.sun.star.util.thePathSettings" else None
    with (
        patch("plugin.draw.designs._resolve_lo_directory_path", return_value=str(presnt)),
        patch("plugin.draw.designs.derive_otp_look") as look,
    ):
        designs = enumerate_impress_designs(ctx, include_look=False)
    look.assert_not_called()
    assert designs[0]["look"] == ""
    assert designs[0]["id"] == "metropolis"


def test_apply_design_missing_enumerates_once():
    ctx = MagicMock()
    ctx.doc.supportsService.side_effect = lambda s: s == "com.sun.star.presentation.PresentationDocument"
    with patch("plugin.draw.designs.enumerate_impress_designs", return_value=[]) as enum:
        out = ApplyDesign().execute(ctx, design="Nope")
    assert out["status"] == "error"
    assert "not found" in out["message"].lower()
    enum.assert_called_once_with(ctx.ctx, include_look=False)


def test_apply_design_disposed_doc_is_not_unsupported():
    class DisposedException(Exception):
        pass

    ctx = MagicMock()
    ctx.doc.supportsService.side_effect = DisposedException("closed")
    with pytest.raises(DisposedException):
        ApplyDesign().execute(ctx, design="Metropolis")


def test_inherit_master_from_neighbor_tries_next_after_failure():
    designed = MagicMock()
    designed.Name = "Designed"
    good = MagicMock()
    good.MasterPage = designed
    new_page = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 3

    def by_index(i):
        if i == 0:
            raise RuntimeError("neighbor broken")
        if i == 1:
            return new_page
        return good

    pages.getByIndex.side_effect = by_index
    name = inherit_master_from_neighbor(pages, new_page, 1)
    assert name == "Designed"
    assert new_page.MasterPage is designed


def test_inherit_master_from_neighbor_reraises_disposed():
    class DisposedException(Exception):
        pass

    pages = MagicMock()
    pages.getCount.return_value = 2
    pages.getByIndex.side_effect = DisposedException("closed")
    with pytest.raises(DisposedException):
        inherit_master_from_neighbor(pages, MagicMock(), 1)

    pages.getCount.side_effect = DisposedException("closed")
    with pytest.raises(DisposedException):
        inherit_master_from_neighbor(pages, MagicMock(), 1)


def test_copy_uno_prop_falls_back_to_attribute_access():
    from plugin.draw.designs import _copy_uno_prop

    class ShapeStub:
        def __init__(self, pos=None):
            self.Position = pos

        def getPropertyValue(self, name):
            raise RuntimeError("UnknownPropertyException")

        def setPropertyValue(self, name, val):
            raise RuntimeError("UnknownPropertyException")

    src = ShapeStub((100, 200))
    dest = ShapeStub((0, 0))
    assert _copy_uno_prop(dest, src, "Position") is True
    assert dest.Position == (100, 200)

    # Missing attribute returns False
    assert _copy_uno_prop(dest, src, "NoSuchProp") is False


def test_decode_odf_name():
    from plugin.draw.designs import _decode_odf_name

    assert _decode_odf_name("Yellow_20_Idea") == "Yellow Idea"
    assert _decode_odf_name("Beehive_20_16_3a_9") == "Beehive 16:9"
    assert _decode_odf_name("Blue_5f_Curve") == "Blue_Curve"
    assert _decode_odf_name("Plain") == "Plain"


def test_synthetic_zip_master_image_extracts_named_href(tmp_path):
    from plugin.draw.designs import _master_image_hrefs_from_zip, clone_master_into_doc

    otp = tmp_path / "MultiPic.otp"
    styles_xml = (
        b'<office:document-styles xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        b'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
        b'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
        b'xmlns:xlink="http://www.w3.org/1999/xlink">'
        b'<style:master-page style:name="Multi_20_Master">'
        b'<draw:image xlink:href="Pictures/c.png"/>'
        b'</style:master-page>'
        b'</office:document-styles>'
    )
    with zipfile.ZipFile(otp, "w") as zf:
        zf.writestr("styles.xml", styles_xml)
        zf.writestr("Pictures/a.png", b"data_a")
        zf.writestr("Pictures/b.png", b"data_b")
        zf.writestr("Pictures/c.png", b"data_c")

    with zipfile.ZipFile(otp) as zf:
        hrefs = _master_image_hrefs_from_zip(zf, "Multi Master")
    assert hrefs == ["Pictures/c.png"]

    dest_doc = MagicMock()
    dest_master = MagicMock()
    dest_master.getCount.return_value = 1
    dest_masters = MagicMock()
    dest_masters.insertNewByIndex.return_value = dest_master
    dest_doc.getMasterPages.return_value = dest_masters

    src_doc = MagicMock()
    src_master = MagicMock()
    src_master.Name = "Multi Master"
    src_shape = MagicMock()
    src_shape.ShapeType = "com.sun.star.drawing.GraphicObjectShape"
    src_master.getCount.return_value = 1
    src_master.getByIndex.return_value = src_shape
    src_masters = MagicMock()
    src_masters.getCount.return_value = 1
    src_masters.getByIndex.return_value = src_master
    src_doc.getMasterPages.return_value = src_masters

    extracted_data: list[bytes] = []

    def fake_reimport(ctx, src_s, clone, *, image_path=None, **kwargs):
        if image_path and Path(image_path).is_file():
            extracted_data.append(Path(image_path).read_bytes())
            return True
        return False

    with patch("plugin.draw.designs._reimport_graphic", side_effect=fake_reimport):
        cloned = clone_master_into_doc(dest_doc, src_doc, {"id": "multi", "name": "Multi Master", "path": str(otp)})

    assert extracted_data == [b"data_c"]
    assert cloned.graphic_shapes == 1
    assert cloned.graphic_reimports == 1


def test_copy_uno_prop_disposal_propagates():
    from plugin.draw.designs import _copy_uno_prop

    class DisposedException(Exception):
        pass

    src = MagicMock()
    src.getPropertyValue.side_effect = DisposedException("disposed")
    dest = MagicMock()

    with pytest.raises(DisposedException):
        _copy_uno_prop(dest, src, "Position")

    src_missing = MagicMock(spec=["getPropertyValue"])
    src_missing.getPropertyValue.side_effect = AttributeError("missing")
    assert _copy_uno_prop(dest, src_missing, "NoSuchProp") is False


def test_copy_uno_prop_geometry_failure_raises():
    from plugin.draw.designs import _copy_uno_prop

    class BrokenDest:
        def setPropertyValue(self, name, val):
            raise RuntimeError("cannot set property")

        def __setattr__(self, name, val):
            if name in ("Position", "Size"):
                raise RuntimeError("cannot set attr")
            super().__setattr__(name, val)

    src = MagicMock()
    src.getPropertyValue.return_value = (10, 20)
    dest = BrokenDest()

    with pytest.raises(RuntimeError, match="Failed to set Position"):
        _copy_uno_prop(dest, src, "Position")


def test_apply_design_chrome_missing_when_zero_reimports(tmp_path):
    dest = MagicMock()
    dest_master = MagicMock()
    dest_master.getCount.return_value = 1
    dest_masters = MagicMock()
    dest_masters.insertNewByIndex.return_value = dest_master
    dest.getMasterPages.return_value = dest_masters

    src = MagicMock()
    src_master = MagicMock()
    src_master.Name = "Metropolis"
    src_shape = MagicMock()
    src_shape.ShapeType = "com.sun.star.drawing.GraphicObjectShape"
    src_master.getCount.return_value = 1
    src_master.getByIndex.return_value = src_shape
    src_masters = MagicMock()
    src_masters.getCount.return_value = 1
    src_masters.getByIndex.return_value = src_master
    src.getMasterPages.return_value = src_masters

    with (
        patch("plugin.draw.designs.open_design_source_hidden", return_value=src),
        patch("plugin.draw.designs._close_hidden_doc"),
        patch("plugin.draw.designs._reimport_graphic", return_value=False),
        patch("plugin.draw.designs.assign_master_to_all_slides") as mock_assign,
    ):
        out = apply_design_to_current_doc(
            MagicMock(),
            dest,
            {"id": "metropolis", "name": "Metropolis", "path": str(tmp_path / "m.otp")},
        )

    assert out["status"] == "error"
    assert out["code"] == "DESIGN_CHROME_MISSING"
    assert out["graphic_shapes"] == 1
    assert out["graphic_reimports"] == 0
    mock_assign.assert_not_called()
    dest_masters.remove.assert_called_with(dest_master)


def test_apply_design_partial_assign_restores_and_removes_clone():
    dest = MagicMock()
    page0 = MagicMock()
    old_m0 = MagicMock()
    page0.MasterPage = old_m0
    page1 = MagicMock()
    old_m1 = MagicMock()
    page1.MasterPage = old_m1

    pages = MagicMock()
    pages.getCount.return_value = 2
    pages.getByIndex.side_effect = lambda i: page0 if i == 0 else page1
    dest.getDrawPages.return_value = pages

    dest_masters = MagicMock()
    dest.getMasterPages.return_value = dest_masters

    src = MagicMock()
    master = MagicMock()
    with (
        patch("plugin.draw.designs.open_design_source_hidden", return_value=src),
        patch("plugin.draw.designs.clone_master_into_doc", return_value=ClonedMaster(master, "Metropolis", 6, 1, 1)),
        patch("plugin.draw.designs._close_hidden_doc"),
        patch("plugin.draw.designs.assign_master_to_all_slides", return_value=1),
        patch("plugin.draw.designs._master_entries", return_value=[]),
    ):
        out = apply_design_to_current_doc(MagicMock(), dest, {"id": "metropolis", "name": "Metropolis"})

    assert out["status"] == "error"
    assert out["code"] == "DESIGN_PARTIAL_APPLY"
    assert out["slides_updated"] == 1
    assert out["slide_count"] == 2
    assert out["rolled_back"] is True
    assert "Rolled back" in out["message"]
    assert out["slides_restored"] == 2
    assert page0.MasterPage == old_m0
    assert page1.MasterPage == old_m1
    dest_masters.remove.assert_called_with(master)


def test_apply_design_assign_exception_rolls_back():
    dest = MagicMock()
    page0 = MagicMock()
    old_m0 = MagicMock()
    page0.MasterPage = old_m0
    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page0
    dest.getDrawPages.return_value = pages
    dest_masters = MagicMock()
    dest.getMasterPages.return_value = dest_masters

    src = MagicMock()
    master = MagicMock()
    with (
        patch("plugin.draw.designs.open_design_source_hidden", return_value=src),
        patch("plugin.draw.designs.clone_master_into_doc", return_value=ClonedMaster(master, "Metropolis", 6, 1, 1)),
        patch("plugin.draw.designs._close_hidden_doc"),
        patch("plugin.draw.designs.assign_master_to_all_slides", side_effect=RuntimeError("assign failed")),
        patch("plugin.draw.designs._master_entries", return_value=[]),
    ):
        out = apply_design_to_current_doc(MagicMock(), dest, {"id": "metropolis", "name": "Metropolis"})

    assert out["status"] == "error"
    assert out["code"] == "DESIGN_PARTIAL_APPLY"
    assert out["rolled_back"] is True
    assert "Rolled back" in out["message"]
    assert page0.MasterPage == old_m0
    dest_masters.remove.assert_called_with(master)


def test_apply_design_assign_disposed_propagates():
    class DisposedException(Exception):
        pass

    dest = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 1
    page0 = MagicMock()
    page0.MasterPage = MagicMock()
    pages.getByIndex.return_value = page0
    dest.getDrawPages.return_value = pages

    src = MagicMock()
    master = MagicMock()
    with (
        patch("plugin.draw.designs.open_design_source_hidden", return_value=src),
        patch("plugin.draw.designs.clone_master_into_doc", return_value=ClonedMaster(master, "Metropolis", 6, 1, 1)),
        patch("plugin.draw.designs._close_hidden_doc"),
        patch("plugin.draw.designs.assign_master_to_all_slides", side_effect=DisposedException("doc disposed")),
    ):
        with pytest.raises(DisposedException):
            apply_design_to_current_doc(MagicMock(), dest, {"id": "metropolis", "name": "Metropolis"})


def test_apply_design_replaces_previous_same_named_master():
    dest = MagicMock()
    m_default = MagicMock()
    m_default.Name = "Default"
    old_metropolis = MagicMock()
    old_metropolis.Name = "Metropolis"
    new_metropolis = MagicMock()
    new_metropolis.Name = "Metropolis_1"

    masters_list = [m_default, old_metropolis]
    dest_masters = MagicMock()
    dest_masters.getCount.side_effect = lambda: len(masters_list)
    dest_masters.getByIndex.side_effect = lambda i: masters_list[i]

    def remove_m(m):
        if m in masters_list:
            masters_list.remove(m)

    dest_masters.remove.side_effect = remove_m
    dest.getMasterPages.return_value = dest_masters

    page0 = MagicMock()
    page0.MasterPage = old_metropolis
    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.return_value = page0
    dest.getDrawPages.return_value = pages

    def do_assign(doc, m):
        page0.MasterPage = m
        return 1

    src = MagicMock()
    with (
        patch("plugin.draw.designs.open_design_source_hidden", return_value=src),
        patch("plugin.draw.designs.clone_master_into_doc", return_value=ClonedMaster(new_metropolis, "Metropolis_1", 6, 1, 1)),
        patch("plugin.draw.designs._close_hidden_doc"),
        patch("plugin.draw.designs.assign_master_to_all_slides", side_effect=do_assign),
    ):
        out = apply_design_to_current_doc(MagicMock(), dest, {"id": "metropolis", "name": "Metropolis"})

    assert out["status"] == "ok"
    assert out["applied_master"] == "Metropolis"
    assert page0.MasterPage is new_metropolis
    assert old_metropolis not in masters_list
    assert m_default in masters_list
    assert new_metropolis.Name == "Metropolis"


def test_enumerate_duplicate_ids_later_directory_wins(tmp_path):
    dir1 = tmp_path / "dir1"
    dir1.mkdir()
    dir2 = tmp_path / "dir2"
    dir2.mkdir()

    f1 = dir1 / "Metropolis.otp"
    f1.write_bytes(b"PK")
    f2 = dir2 / "Metropolis.otp"
    f2.write_bytes(b"PK")

    class MockCtx:
        pass

    with patch("plugin.draw.designs._iter_template_directories", return_value=[str(dir1), str(dir2)]):
        designs = enumerate_impress_designs(MockCtx(), include_look=False)

    assert len(designs) == 1
    assert designs[0]["id"] == "metropolis"
    assert designs[0]["path"] == str(f2)


def test_enumerate_case_collision_in_single_dir_resolves_to_one(tmp_path):
    d = tmp_path / "templates"
    d.mkdir()
    (d / "A.otp").write_bytes(b"PK")
    try:
        (d / "a.otp").write_bytes(b"PK")
    except OSError:
        pass

    class MockCtx:
        pass

    with patch("plugin.draw.designs._iter_template_directories", return_value=[str(d)]):
        designs = enumerate_impress_designs(MockCtx(), include_look=False)

    assert len(designs) == 1
    assert designs[0]["id"] == "a"


def test_apply_design_snapshot_exception_rolls_back_and_removes_clone():
    dest = MagicMock()
    page0 = MagicMock()
    old_m0 = MagicMock()
    page0.MasterPage = old_m0
    page1 = MagicMock()
    old_m1 = MagicMock()
    page1.MasterPage = old_m1
    pages = MagicMock()
    pages.getCount.return_value = 2

    calls = 0

    def by_index(i):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("snapshot page access failed")
        return page0 if i == 0 else page1

    pages.getByIndex.side_effect = by_index
    dest.getDrawPages.return_value = pages
    dest_masters = MagicMock()
    dest.getMasterPages.return_value = dest_masters

    src = MagicMock()
    master = MagicMock()
    with (
        patch("plugin.draw.designs.open_design_source_hidden", return_value=src),
        patch("plugin.draw.designs.clone_master_into_doc", return_value=ClonedMaster(master, "Metropolis", 6, 1, 1)),
        patch("plugin.draw.designs._close_hidden_doc"),
        patch("plugin.draw.designs._master_entries", return_value=[]),
    ):
        out = apply_design_to_current_doc(MagicMock(), dest, {"id": "metropolis", "name": "Metropolis"})

    assert out["status"] == "error"
    assert out["code"] == "DESIGN_PARTIAL_APPLY"
    assert out["rolled_back"] is True
    assert "Rolled back" in out["message"]
    assert page0.MasterPage == old_m0
    assert page1.MasterPage == old_m1
    dest_masters.remove.assert_called_with(master)


def test_is_master_referenced_error_handling():
    from plugin.draw.designs import _cleanup_previous_same_named_masters, _is_master_referenced

    class DisposedException(Exception):
        pass

    doc = MagicMock()
    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex.side_effect = RuntimeError("unknown error")
    doc.getDrawPages.return_value = pages

    master = MagicMock()
    # On unknown error, returns True (safe: does not treat as unreferenced)
    assert _is_master_referenced(doc, master) is True

    old_m = MagicMock()
    old_m.Name = "Metropolis"
    dest_masters = MagicMock()
    dest_masters.getCount.return_value = 1
    dest_masters.getByIndex.return_value = old_m
    doc.getMasterPages.return_value = dest_masters
    _cleanup_previous_same_named_masters(doc, master, "Metropolis")
    dest_masters.remove.assert_not_called()

    # DisposedException propagates
    pages.getByIndex.side_effect = DisposedException("disposed")
    with pytest.raises(DisposedException):
        _is_master_referenced(doc, master)


def test_master_image_href_count_mismatch_and_multi_image(tmp_path):
    from plugin.draw.designs import _master_image_hrefs_from_zip, clone_master_into_doc

    otp = tmp_path / "TwoHref.otp"
    styles_xml = (
        b'<office:document-styles xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        b'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
        b'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
        b'xmlns:xlink="http://www.w3.org/1999/xlink">'
        b'<style:master-page style:name="Multi Master">'
        b'<draw:image xlink:href="Pictures/p1.png"/>'
        b'<draw:image xlink:href="Pictures/p2.png"/>'
        b'</style:master-page>'
        b'</office:document-styles>'
    )
    with zipfile.ZipFile(otp, "w") as zf:
        zf.writestr("styles.xml", styles_xml)
        zf.writestr("Pictures/p1.png", b"pic1_data")
        zf.writestr("Pictures/p2.png", b"pic2_data")

    # Entity bomb in styles.xml returns None
    bomb_otp = tmp_path / "Bomb.otp"
    bomb_styles = b'<!DOCTYPE styles [ <!ENTITY xxe "bomb"> ]><office:document-styles/>'
    with zipfile.ZipFile(bomb_otp, "w") as zf:
        zf.writestr("styles.xml", bomb_styles)
    with zipfile.ZipFile(bomb_otp) as zf:
        assert _master_image_hrefs_from_zip(zf, "Any") is None

    # Normal master hrefs still return
    with zipfile.ZipFile(otp) as zf:
        assert _master_image_hrefs_from_zip(zf, "Multi Master") == ["Pictures/p1.png", "Pictures/p2.png"]

    # 1 href and 2 shapes: mismatch -> image_path=None for both
    dest_doc = MagicMock()
    dest_master = MagicMock()
    dest_master.getCount.return_value = 2
    dest_masters = MagicMock()
    dest_masters.insertNewByIndex.return_value = dest_master
    dest_doc.getMasterPages.return_value = dest_masters

    src_doc = MagicMock()
    src_master = MagicMock()
    src_master.Name = "Single Master"
    s1 = MagicMock()
    s1.ShapeType = "com.sun.star.drawing.GraphicObjectShape"
    s2 = MagicMock()
    s2.ShapeType = "com.sun.star.drawing.GraphicObjectShape"
    src_master.getCount.return_value = 2
    src_master.getByIndex.side_effect = lambda i: s1 if i == 0 else s2
    src_masters = MagicMock()
    src_masters.getCount.return_value = 1
    src_masters.getByIndex.return_value = src_master
    src_doc.getMasterPages.return_value = src_masters

    single_otp = tmp_path / "SingleHref.otp"
    single_styles = (
        b'<office:document-styles xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        b'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
        b'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
        b'xmlns:xlink="http://www.w3.org/1999/xlink">'
        b'<style:master-page style:name="Single Master">'
        b'<draw:image xlink:href="Pictures/p1.png"/>'
        b'</style:master-page>'
        b'</office:document-styles>'
    )
    with zipfile.ZipFile(single_otp, "w") as zf:
        zf.writestr("styles.xml", single_styles)
        zf.writestr("Pictures/p1.png", b"pic1_data")

    reimport_calls: list[str | None] = []

    def record_reimport(ctx, src_s, clone, *, image_path=None, **kwargs):
        reimport_calls.append(image_path)
        return True

    with patch("plugin.draw.designs._reimport_graphic", side_effect=record_reimport):
        clone_master_into_doc(dest_doc, src_doc, {"id": "single", "name": "Single Master", "path": str(single_otp)})
    assert reimport_calls == [None, None]

    # 2 hrefs and 2 shapes: each gets its own file
    src_master.Name = "Multi Master"
    reimport_files: list[bytes] = []

    def record_files(ctx, src_s, clone, *, image_path=None, **kwargs):
        if image_path and Path(image_path).is_file():
            reimport_files.append(Path(image_path).read_bytes())
            return True
        return False

    with patch("plugin.draw.designs._reimport_graphic", side_effect=record_files):
        cloned = clone_master_into_doc(dest_doc, src_doc, {"id": "multi", "name": "Multi Master", "path": str(otp)})
    assert reimport_files == [b"pic1_data", b"pic2_data"]
    assert cloned.graphic_shapes == 2
    assert cloned.graphic_reimports == 2


def test_apply_design_partial_chrome_message():
    dest = MagicMock()
    dest.getDrawPages().getCount.return_value = 1
    page0 = MagicMock()
    dest.getDrawPages().getByIndex.return_value = page0
    src = MagicMock()
    master = MagicMock()
    with (
        patch("plugin.draw.designs.open_design_source_hidden", return_value=src),
        patch("plugin.draw.designs.clone_master_into_doc", return_value=ClonedMaster(master, "Metropolis", 6, 2, 1)),
        patch("plugin.draw.designs._close_hidden_doc"),
        patch("plugin.draw.designs.assign_master_to_all_slides", return_value=1),
        patch("plugin.draw.designs._master_entries", return_value=[]),
    ):
        out = apply_design_to_current_doc(MagicMock(), dest, {"id": "metropolis", "name": "Metropolis"})
    assert out["status"] == "ok"
    assert "1 of 2 chrome shapes imported" in out["message"]



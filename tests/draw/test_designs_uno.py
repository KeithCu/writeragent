# WriterAgent — native UNO tests for Impress list/apply design (M1′)
# SPDX-License-Identifier: GPL-3.0-or-later
"""Prove PathSettings list, current-doc master import, and that the hidden design source is a separate scratch document."""

from __future__ import annotations

import json

from plugin.testing_runner import native_test
try:
    from plugin.tests.testing_utils import TestingFactory, with_native_doc  # ty: ignore[unresolved-import]
except ImportError:
    from tests.testing_utils import TestingFactory, with_native_doc


def _exec(doc, ctx, name, args, doc_type="impress"):
    res = TestingFactory.execute_tool(doc, ctx, name, args, doc_type=doc_type)
    return res if isinstance(res, dict) else json.loads(res)


def _pick_known_design(listed):
    designs = listed.get("designs") or []
    for d in designs:
        name = "%s %s" % (d.get("id") or "", d.get("name") or "")
        if "metropolis" in name.lower():
            return d
    return designs[0] if designs else None


@native_test
@with_native_doc("impress")
def test_list_designs_finds_metropolis(ctx, doc):
    listed = _exec(doc, ctx, "design_list", {})
    assert listed.get("status") == "ok", listed
    assert listed.get("count", 0) >= 1, listed
    ids = [str(d.get("id") or "") for d in listed.get("designs") or []]
    names = [str(d.get("name") or "") for d in listed.get("designs") or []]
    blob = " ".join(ids + names).lower()
    assert "metropolis" in blob, "design_list did not find Metropolis: %s" % listed
    for d in listed.get("designs") or []:
        assert d.get("path"), d
        assert d.get("url", "").startswith("file:"), d
        assert "look" in d, d
    metro = next((d for d in listed.get("designs") or [] if "metropolis" in str(d.get("id") or "").lower()), None)
    assert metro is not None, listed
    look = str(metro.get("look") or "")
    assert "dark" in look.lower(), metro
    assert "blue" in look.lower(), metro


@native_test
@with_native_doc("impress")
def test_apply_design_current_doc_metropolis(ctx, doc):
    """Hidden .otp master clone + MasterPage assign restyles the open deck.

    Pollutes the system clipboard first: headed #791 DiaMode Paste pulled
    desktop junk instead of the Hidden template. Clone must ignore that.
    """
    from plugin.chatbot.dialogs import copy_to_clipboard

    copy_to_clipboard(ctx, "SPREADSHEET_AUDIT_JUNK_SHOULD_NOT_APPEAR")
    listed = _exec(doc, ctx, "design_list", {})
    design = _pick_known_design(listed)
    assert design, listed
    added = _exec(doc, ctx, "slide_add", {})
    assert added.get("status") == "ok", added
    layout0 = _exec(doc, ctx, "slide_set_layout", {"page": 0, "layout": "text"})
    assert layout0.get("status") == "ok", layout0
    title0 = _exec(doc, ctx, "placeholder_set_text", {"page": 0, "role": "title", "text": "Keep Title 0"})
    if title0.get("status") != "ok":
        title0 = _exec(doc, ctx, "placeholder_set_text", {"page": 0, "index": 0, "text": "Keep Title 0"})
    assert title0.get("status") == "ok", title0
    title1 = _exec(
        doc, ctx, "placeholder_set_text", {"page": 1, "role": "title", "text": "Keep Title 1"}
    )
    body1 = _exec(doc, ctx, "placeholder_set_text", {"page": 1, "role": "body", "text": "Keep Body 1"})
    assert title1.get("status") == "ok", title1
    assert body1.get("status") == "ok", body1
    before_count = doc.getDrawPages().getCount()
    assert before_count >= 2, before_count

    out = _exec(doc, ctx, "design_apply", {"design": design["id"]})
    assert out.get("status") == "ok", out
    assert out.get("import_method") == "clone_master", out
    assert out.get("blank_master") is False, out
    applied = str(out.get("applied_master") or "")
    assert applied, out
    assert applied.lower() != "default", out
    assert int(out.get("applied_master_shape_count") or 0) >= 6, out
    assert int(out.get("slides_updated") or 0) == before_count, out
    assert isinstance(out.get("clone_warnings"), list), out
    assert isinstance(out.get("clone_warning_count"), int), out
    pages = doc.getDrawPages()
    assert pages.getCount() == before_count, "slide count changed: %s out=%s" % (
        pages.getCount(),
        out,
    )
    title_w = None
    has_graphic = False
    for i in range(pages.getCount()):
        master = pages.getByIndex(i).MasterPage
        name = master.Name if hasattr(master, "Name") else ""
        assert name == applied, "slide %s master=%s applied=%s out=%s" % (i, name, applied, out)
        shapes = 0
        try:
            shapes = int(master.getCount())
        except Exception:
            shapes = 0
        assert shapes >= 6, "slide %s master shape_count=%s out=%s" % (i, shapes, out)
        for j in range(shapes):
            sh = master.getByIndex(j)
            st = str(getattr(sh, "ShapeType", "") or "")
            if "GraphicObject" in st:
                has_graphic = True
            if "TitleText" in st:
                try:
                    title_w = int(sh.Size.Width)
                except Exception:
                    title_w = None
    assert has_graphic, "cloned master missing GraphicObjectShape chrome: %s" % out
    if "metropolis" in str(design.get("id") or "").lower() and title_w is not None:
        # Probe: factory Default title is 25200; Metropolis chrome is 14800.
        assert title_w == 14800, "Metropolis title width %s (not cloned geom) out=%s" % (
            title_w,
            out,
        )

    kept0 = _exec(doc, ctx, "placeholder_get_text", {"page": 0, "role": "title"})
    if kept0.get("status") != "ok":
        kept0 = _exec(doc, ctx, "placeholder_get_text", {"page": 0, "index": 0})
    kept1 = _exec(doc, ctx, "placeholder_get_text", {"page": 1, "role": "title"})
    kept_body = _exec(doc, ctx, "placeholder_get_text", {"page": 1, "role": "body"})
    assert "Keep Title 0" in str(kept0.get("text") or ""), kept0
    assert "Keep Title 1" in str(kept1.get("text") or ""), kept1
    assert "Keep Body 1" in str(kept_body.get("text") or ""), kept_body
    junk_blob = "%s %s %s %s" % (kept0, kept1, kept_body, out)
    for i in range(pages.getCount()):
        page = pages.getByIndex(i)
        for j in range(int(page.getCount())):
            try:
                junk_blob += " " + str(page.getByIndex(j).String or "")
            except Exception:
                continue
    assert "SPREADSHEET_AUDIT_JUNK" not in junk_blob, junk_blob


@native_test
@with_native_doc("impress")
def test_design_source_is_a_separate_scratch_document_uno(ctx, doc):
    from plugin.draw.designs import (
        _blank_master_signal,
        _master_entries,
        open_design_source_hidden,
    )
    from plugin.framework.scratch_writer import close_scratch_document, is_scratch_document
    from plugin.framework.uno_context import get_runtime_uid

    listed = _exec(doc, ctx, "design_list", {})
    design = _pick_known_design(listed)
    assert design, listed
    factory = doc.getMasterPages().getByIndex(0)
    factory_name = factory.Name if hasattr(factory, "Name") else ""

    src1 = open_design_source_hidden(ctx, design)
    src2 = None
    try:
        src2 = open_design_source_hidden(ctx, design)
        assert get_runtime_uid(src1) != get_runtime_uid(src2)
        assert src1.getURL() == ""
        assert src2.getURL() == ""
        assert is_scratch_document(src1)
        assert is_scratch_document(src2)
        masters = _master_entries(src1)
        assert _blank_master_signal(masters) is False, masters
        open_master = doc.getMasterPages().getByIndex(0)
        open_name = open_master.Name if hasattr(open_master, "Name") else ""
        assert open_name == factory_name, "open deck master changed: %s vs %s" % (
            open_name,
            factory_name,
        )
    finally:
        if src2 is not None:
            close_scratch_document(src2)
        close_scratch_document(src1)


@native_test
@with_native_doc("draw")
def test_apply_design_draw_not_impress(ctx, doc):
    out = _exec(doc, ctx, "design_apply", {"design": "Metropolis"}, doc_type="draw")
    assert out.get("status") == "error", out
    assert out.get("code") == "UNSUPPORTED_DOC_TYPE", out


@native_test
@with_native_doc("impress")
def test_apply_design_clones_shipped_template_chrome(ctx, doc):
    from plugin.draw.designs import open_design_source_hidden
    from plugin.framework.scratch_writer import close_scratch_document

    listed = _exec(doc, ctx, "design_list", {})
    designs_by_id = {d["id"]: d for d in (listed.get("designs") or [])}

    # Sunset
    if "sunset" in designs_by_id:
        hidden_doc = open_design_source_hidden(ctx, designs_by_id["sunset"])
        try:
            expected_bg_color = None
            src_m = hidden_doc.getMasterPages().getByIndex(0)
            if hasattr(src_m, "Background") and hasattr(src_m.Background, "FillColor"):
                expected_bg_color = src_m.Background.FillColor
        finally:
            close_scratch_document(hidden_doc)

        out_sunset = _exec(doc, ctx, "design_apply", {"design": "sunset"})
        assert out_sunset.get("status") == "ok", out_sunset
        master = doc.getDrawPages().getByIndex(0).MasterPage
        if expected_bg_color is not None and hasattr(master, "Background") and hasattr(master.Background, "FillColor"):
            assert master.Background.FillColor == expected_bg_color
        has_group_with_children = False
        for j in range(int(master.getCount())):
            sh = master.getByIndex(j)
            if "GroupShape" in str(getattr(sh, "ShapeType", "") or ""):
                if int(sh.getCount()) > 0:
                    has_group_with_children = True
                    break
        assert has_group_with_children, "Sunset cloned master missing populated GroupShape"

        # Reapply Sunset: verify cleanup of previous same-named master
        out_reapply = _exec(doc, ctx, "design_apply", {"design": "sunset"})
        assert out_reapply.get("status") == "ok", out_reapply
        sunset_masters = [
            doc.getMasterPages().getByIndex(k)
            for k in range(doc.getMasterPages().getCount())
            if str(getattr(doc.getMasterPages().getByIndex(k), "Name", "") or "").lower() == "sunset"
        ]
        assert len(sunset_masters) == 1

    # Beehive
    if "beehive" in designs_by_id:
        out_bee = _exec(doc, ctx, "design_apply", {"design": "beehive"})
        assert out_bee.get("status") == "ok", out_bee
        master = doc.getDrawPages().getByIndex(0).MasterPage
        has_bee_group = False
        for j in range(int(master.getCount())):
            sh = master.getByIndex(j)
            if "GroupShape" in str(getattr(sh, "ShapeType", "") or ""):
                # On macOS LibreOffice, some shapes with GroupShape in ShapeType
                # raise com.sun.star.uno.RuntimeException on getCount() or lack it;
                # treat them as unpopulated groups and keep looking for the cloned group chrome.
                try:
                    count = int(sh.getCount()) if hasattr(sh, "getCount") else 0
                except Exception:
                    count = 0
                if count > 0:
                    has_bee_group = True
                    break
        assert has_bee_group, "Beehive cloned master missing populated GroupShape"

    # Vivid
    if "vivid" in designs_by_id:
        out_vivid = _exec(doc, ctx, "design_apply", {"design": "vivid"})
        assert out_vivid.get("status") == "ok", out_vivid
        master = doc.getDrawPages().getByIndex(0).MasterPage
        has_geom = False
        for j in range(int(master.getCount())):
            sh = master.getByIndex(j)
            if "CustomShape" in str(getattr(sh, "ShapeType", "") or ""):
                geom = getattr(sh, "CustomShapeGeometry", None)
                if geom:
                    has_geom = True
                    break
        assert has_geom, "Vivid cloned master missing non-empty CustomShapeGeometry"

    # Grey Elegant
    if "grey_elegant" in designs_by_id or "grey-elegant" in designs_by_id:
        did = "grey_elegant" if "grey_elegant" in designs_by_id else "grey-elegant"
        out_grey = _exec(doc, ctx, "design_apply", {"design": did})
        assert out_grey.get("status") == "ok", out_grey
        applied = str(out_grey.get("applied_master") or "").lower()
        assert "grey" in applied and "elegant" in applied


@native_test
@with_native_doc("impress")
def test_apply_design_aspect_ratio_selection(ctx, doc):
    listed = _exec(doc, ctx, "design_list", {})
    available_ids = [d["id"] for d in (listed.get("designs") or [])]
    if "beehive" not in available_ids:
        return

    # Set page 0 to 4:3 (28000 x 21000)
    p0 = doc.getDrawPages().getByIndex(0)
    p0.Width = 28000
    p0.Height = 21000

    out = _exec(doc, ctx, "design_apply", {"design": "beehive"})
    assert out.get("status") == "ok", out
    master = p0.MasterPage
    mw = getattr(master, "Width", 0)
    mh = getattr(master, "Height", 0)
    if mw and mh:
        ratio = float(mw) / float(mh)
        assert abs(ratio - (4.0 / 3.0)) < 0.05, f"Expected 4:3 master ratio, got {mw}x{mh} ({ratio})"


@native_test
@with_native_doc("impress")
def test_apply_design_growing_liberty_fallback(ctx, doc):
    listed = _exec(doc, ctx, "design_list", {})
    available_ids = [d["id"] for d in (listed.get("designs") or [])]
    target_id = next((i for i in available_ids if "growing" in i and "liberty" in i), None)
    if not target_id:
        return

    out = _exec(doc, ctx, "design_apply", {"design": target_id})
    assert out.get("status") == "ok", out
    assert "Title" in str(out.get("source_master") or ""), out
    assert out.get("selection") == "fallback", out
    warnings = out.get("clone_warnings") or []
    assert any("no master name matched" in str(w) for w in warnings), out
    applied = str(out.get("applied_master") or "").lower()
    assert "growing" in applied and "liberty" in applied, out

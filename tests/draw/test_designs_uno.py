# WriterAgent — native UNO tests for Impress list/apply design (M1′)
# SPDX-License-Identifier: GPL-3.0-or-later
"""Prove PathSettings list, create-from-template, current-doc master import, HF wire-up."""

from __future__ import annotations

import json

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc


def _exec(doc, ctx, name, args, doc_type="impress"):
    res = TestingFactory.execute_tool(doc, ctx, name, args, doc_type=doc_type)
    return res if isinstance(res, dict) else json.loads(res)


def _close_created(ctx, result):
    """Close a presentation opened by create-from-template so suites do not leak frames."""
    uid = (result or {}).get("document_uid") or ""
    url = (result or {}).get("document_url") or ""
    key = uid or url
    if not key:
        return
    try:
        from plugin.framework.uno_context import resolve_document_by_url

        model, _unused = resolve_document_by_url(ctx, key)
    except Exception:
        model = None
    if model is not None:
        TestingFactory.close_doc(model)


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
    listed = _exec(doc, ctx, "list_designs", {})
    assert listed.get("status") == "ok", listed
    assert listed.get("count", 0) >= 1, listed
    ids = [str(d.get("id") or "") for d in listed.get("designs") or []]
    names = [str(d.get("name") or "") for d in listed.get("designs") or []]
    blob = " ".join(ids + names).lower()
    assert "metropolis" in blob, "list_designs did not find Metropolis: %s" % listed
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
    listed = _exec(doc, ctx, "list_designs", {})
    design = _pick_known_design(listed)
    assert design, listed
    added = _exec(doc, ctx, "add_slide", {})
    assert added.get("status") == "ok", added
    layout0 = _exec(doc, ctx, "set_slide_layout", {"page": 0, "layout": "text"})
    assert layout0.get("status") == "ok", layout0
    title0 = _exec(doc, ctx, "set_placeholder_text", {"page": 0, "role": "title", "text": "Keep Title 0"})
    if title0.get("status") != "ok":
        title0 = _exec(doc, ctx, "set_placeholder_text", {"page": 0, "index": 0, "text": "Keep Title 0"})
    assert title0.get("status") == "ok", title0
    title1 = _exec(
        doc, ctx, "set_placeholder_text", {"page": 1, "role": "title", "text": "Keep Title 1"}
    )
    body1 = _exec(doc, ctx, "set_placeholder_text", {"page": 1, "role": "body", "text": "Keep Body 1"})
    assert title1.get("status") == "ok", title1
    assert body1.get("status") == "ok", body1
    before_count = doc.getDrawPages().getCount()
    assert before_count >= 2, before_count

    out = _exec(doc, ctx, "apply_design", {"design": design["id"], "new_document": False})
    assert out.get("status") == "ok", out
    assert out.get("import_method") == "clone_master", out
    assert out.get("blank_master") is False, out
    applied = str(out.get("applied_master") or "")
    assert applied, out
    assert applied.lower() != "default", out
    assert int(out.get("applied_master_shape_count") or 0) >= 6, out
    assert int(out.get("slides_updated") or 0) == before_count, out
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

    kept0 = _exec(doc, ctx, "get_placeholder_text", {"page": 0, "role": "title"})
    if kept0.get("status") != "ok":
        kept0 = _exec(doc, ctx, "get_placeholder_text", {"page": 0, "index": 0})
    kept1 = _exec(doc, ctx, "get_placeholder_text", {"page": 1, "role": "title"})
    kept_body = _exec(doc, ctx, "get_placeholder_text", {"page": 1, "role": "body"})
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
def test_create_from_template_non_default_master(ctx, doc):
    listed = _exec(doc, ctx, "list_designs", {})
    design = _pick_known_design(listed)
    assert design, listed
    factory = doc.getMasterPages().getByIndex(0)
    factory_name = factory.Name if hasattr(factory, "Name") else ""
    out = _exec(doc, ctx, "apply_design", {"design": design["id"], "new_document": True, "hidden": True})
    try:
        assert out.get("status") == "ok", out
        assert out.get("blank_master") is False, out
        names = [str(m.get("name") or "") for m in out.get("masters") or []]
        assert names, out
        assert any(n and n.lower() != "default" for n in names) or any(
            int(m.get("shape_count") or 0) >= 3 for m in out.get("masters") or []
        ), "new-doc-from-template still looks Default: factory=%s out=%s" % (factory_name, out)
    finally:
        _close_created(ctx, out)


@native_test
@with_native_doc("impress")
def test_set_presentation_design_enables_hf(ctx, doc):
    listed = _exec(doc, ctx, "list_designs", {})
    design = _pick_known_design(listed)
    assert design, listed
    out = _exec(doc, ctx, "set_presentation_design", {"design": design["id"], "hidden": True})
    try:
        assert out.get("status") == "ok", out
        hf = out.get("headers_footers") or {}
        master = hf.get("master") or {}
        slide = hf.get("slide") or {}
        assert master.get("status") == "ok", out
        assert slide.get("status") == "ok", out
        assert int(master.get("updated_properties") or 0) + int(slide.get("updated_properties") or 0) > 0, out
        uid = out.get("document_uid")
        from plugin.framework.uno_context import resolve_document_by_url

        new_doc, _unused = resolve_document_by_url(ctx, uid)
        assert new_doc is not None, out
        verify = _exec(new_doc, ctx, "get_headers_footers", {"page": 0, "is_master_page": True})
        assert verify.get("status") == "ok", verify
        props = verify.get("properties") or {}
        assert props.get("IsPageNumberVisible") is True or props.get("IsFooterVisible") is True, verify
        added = _exec(new_doc, ctx, "add_slide", {})
        assert added.get("status") == "ok", added
        pages = new_doc.getDrawPages()
        m0 = pages.getByIndex(0).MasterPage
        m1 = pages.getByIndex(added["active_page_index"]).MasterPage
        n0 = m0.Name if hasattr(m0, "Name") else ""
        n1 = m1.Name if hasattr(m1, "Name") else ""
        assert n0 == n1, "add_slide did not inherit master: %s vs %s added=%s" % (n0, n1, added)
    finally:
        _close_created(ctx, out)


@native_test
@with_native_doc("draw")
def test_set_presentation_design_draw_not_impress(ctx, doc):
    out = _exec(doc, ctx, "set_presentation_design", {"design": "Metropolis"}, doc_type="draw")
    assert out.get("status") == "error", out
    assert out.get("code") == "UNSUPPORTED_DOC_TYPE", out

# WriterAgent — native UNO tests for Impress list/apply design (M1′)
# SPDX-License-Identifier: GPL-3.0-or-later
"""Prove PathSettings list, current-doc master import, and the internal create-from-template helper."""

from __future__ import annotations

import json

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc


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

    out = _exec(doc, ctx, "apply_design", {"design": design["id"]})
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
def test_create_from_template_helper_non_default_master(ctx, doc):
    """Internal helper only — apply_design must not open this new-doc path."""
    from plugin.draw.designs import (
        _blank_master_signal,
        _master_entries,
        create_presentation_from_design,
        resolve_design,
    )

    listed = _exec(doc, ctx, "list_designs", {})
    design = _pick_known_design(listed)
    assert design, listed
    entry = resolve_design(ctx, design["id"])
    assert entry, listed
    factory = doc.getMasterPages().getByIndex(0)
    factory_name = factory.Name if hasattr(factory, "Name") else ""
    new_doc = create_presentation_from_design(ctx, entry, hidden=True)
    try:
        masters = _master_entries(new_doc)
        assert _blank_master_signal(masters) is False, masters
        names = [str(m.get("name") or "") for m in masters]
        assert names, masters
        assert any(n and n.lower() != "default" for n in names) or any(
            int(m.get("shape_count") or 0) >= 3 for m in masters
        ), "create-from-template still looks Default: factory=%s masters=%s" % (
            factory_name,
            masters,
        )
        assert doc.getDrawPages().getCount() >= 1
        # The open deck is unchanged — helper opened a separate Hidden model.
        open_master = doc.getMasterPages().getByIndex(0)
        open_name = open_master.Name if hasattr(open_master, "Name") else ""
        assert open_name == factory_name, "open deck master changed: %s vs %s" % (
            open_name,
            factory_name,
        )
    finally:
        TestingFactory.close_doc(new_doc)


@native_test
@with_native_doc("draw")
def test_apply_design_draw_not_impress(ctx, doc):
    out = _exec(doc, ctx, "apply_design", {"design": "Metropolis"}, doc_type="draw")
    assert out.get("status") == "error", out
    assert out.get("code") == "UNSUPPORTED_DOC_TYPE", out

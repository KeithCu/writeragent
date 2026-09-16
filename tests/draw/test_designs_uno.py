# WriterAgent — native UNO tests for Impress list/apply design (M1′)
# SPDX-License-Identifier: GPL-3.0-or-later
"""Prove PathSettings list, create-from-template, LO-wall current-doc, HF wire-up."""

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
def test_apply_design_current_doc_lo_wall(ctx, doc):
    listed = _exec(doc, ctx, "list_designs", {})
    design = _pick_known_design(listed)
    assert design, listed
    out = _exec(doc, ctx, "apply_design", {"design": design["id"], "new_document": False})
    assert out.get("status") == "error", out
    assert out.get("code") == "LO_WALL", out
    details = out.get("details") or {}
    assert details.get("reason") == "current_doc_apply_unsupported", out
    assert "loadStylesFromURL" in (out.get("message") or "")


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

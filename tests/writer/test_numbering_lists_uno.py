# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""List-only multi-level numbering after apply_document_content HTML.

Scope is nested Writer *lists* (NumberingRules / NumberingLevel / ListId /
generated labels). This suite does not enable or assert Tools → Chapter
Numbering (heading outline numbers). There is no numbering_inspect tool —
tests read UNO paragraph props and XHTML export directly.

Live lock (StarWriter HTML import on a default Writer doc):
- Nested ``<ol>`` items share one ListId and NumberingStyleName; levels are 0/1/2.
- ListLabelString and XHTML ``Numbering_20_Symbols`` show ``1.`` / ``2.`` —
  nested levels are *not* hierarchical ``1.2.3``.
- A second ``<ol>`` after a body paragraph is a new ListId and starts at ``1.``
  (ParaIsNumberingRestart stays false; restart is the new list instance).
- ``search_in_document("1.")`` / ``("1.2")`` miss generated labels (known
  XSearchable blind spot). Item *text* still matches.
- Title-only search replace keeps NumberingLevel / style / ListId on the item.
- Mixed ``<ul>`` + nested ``<ol>`` works; the following normal paragraph is
  not a leftover list item.
"""

from __future__ import annotations

import re

import uno  # noqa: F401

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import (
    TestingFactory,
    skip_windows_leftover_hidden_load,
    with_native_doc,
)
from plugin.writer.content import ApplyDocumentContent, GetDocumentContent
from plugin.writer.search import SearchInDocument

# com.sun.star.style.NumberingType — same constants as rich_text_paste.
_NUMBERING_ARABIC = 4
_NUMBERING_CHAR_SPECIAL = 6

_NUMBERING_SYMBOL_SPAN = re.compile(
    r'class="Numbering_20_Symbols"[^>]*>([^<]*)'
)
_BULLET_SYMBOL_SPAN = re.compile(
    r'class="Bullet_20_Symbols"[^>]*>([^<]*)'
)

# In-test fixture (no committed .fodt). One HTML fragment per block so a nested
# <ol>/<ul> stays one StarWriter import and keeps NumberingLevel.
_LIST_FIXTURE_HTML = [
    "<h2>List numbering boundary</h2>",
    (
        "<ol>"
        "<li>Outer one"
        "<ol><li>Nested two"
        "<ol><li>Nested three</li></ol>"
        "</li></ol>"
        "</li>"
        "<li>Outer two</li>"
        "</ol>"
    ),
    "<p>Body paragraph after nested list.</p>",
    "<ol><li>Restarted alpha</li><li>Restarted beta</li></ol>",
    (
        "<ul><li>Bullet parent"
        "<ol><li>Nested numbered under bullet</li></ol>"
        "</li></ul>"
    ),
    "<p>Normal paragraph after mixed list.</p>",
]


def _tool_ctx(doc, ctx):
    # apply_document_content HTML import opens a Hidden StarWriter temp doc.
    # Same leftover-Hidden skip as other apply_document_content UNO suites.
    skip_windows_leftover_hidden_load("apply_document_content Hidden _default swriter")
    return TestingFactory.create_context(doc=doc, ctx=ctx, env="native")


def _rule_prop(rules, level, name):
    """Read one NumberingRules level property (NumberingType, BulletChar, …)."""
    if rules is None:
        return None
    try:
        props = rules.getByIndex(int(level))
    except Exception:
        return None
    for prop in props:
        if getattr(prop, "Name", None) == name:
            return prop.Value
    return None


def _iter_paras(doc):
    """Non-empty body paragraphs with list props. Trailing empty Standard is skipped."""
    enum = doc.getText().createEnumeration()
    while enum.hasMoreElements():
        el = enum.nextElement()
        try:
            if hasattr(el, "supportsService") and not el.supportsService(
                "com.sun.star.text.Paragraph"
            ):
                continue
        except Exception:
            continue
        text = str(el.getString() or "")
        if not text.strip():
            continue
        level = el.getPropertyValue("NumberingLevel") or 0
        rules = el.getPropertyValue("NumberingRules")
        yield {
            "text": text,
            "ParaStyleName": str(el.getPropertyValue("ParaStyleName") or ""),
            "NumberingStyleName": str(el.getPropertyValue("NumberingStyleName") or ""),
            "NumberingLevel": int(level),
            "ListId": str(el.getPropertyValue("ListId") or ""),
            "ListLabelString": str(el.getPropertyValue("ListLabelString") or ""),
            "ParaIsNumberingRestart": bool(el.getPropertyValue("ParaIsNumberingRestart")),
            "NumberingIsNumber": bool(el.getPropertyValue("NumberingIsNumber")),
            "NumberingRules": rules,
            "NumberingType": _rule_prop(rules, level, "NumberingType"),
        }


def _para(doc, needle):
    snaps = list(_iter_paras(doc))
    for snap in snaps:
        if snap["text"] == needle:
            return snap
    raise AssertionError(
        "paragraph %r not found; texts=%s" % (needle, [s["text"] for s in snaps])
    )


def _apply_list_fixture(doc, ctx):
    tool_ctx = _tool_ctx(doc, ctx)
    res = ApplyDocumentContent().execute(
        tool_ctx, target="full_document", content=_LIST_FIXTURE_HTML
    )
    assert res.get("status") == "ok", res
    return tool_ctx


def _assert_not_list_item(snap, label):
    """Body/heading paragraphs after a list must not inherit leftover NumberingRules."""
    assert not (snap["NumberingStyleName"] or "").strip(), (
        "%s still has NumberingStyleName=%r" % (label, snap["NumberingStyleName"])
    )
    assert not (snap["ListId"] or "").strip(), (
        "%s still has ListId=%r" % (label, snap["ListId"])
    )
    assert not (snap["ListLabelString"] or "").strip(), (
        "%s still has ListLabelString=%r" % (label, snap["ListLabelString"])
    )
    assert snap["NumberingRules"] is None, "%s still has NumberingRules" % label
    assert snap["NumberingIsNumber"] is False, label


def _assert_list_item(
    snap,
    *,
    level,
    label=None,
    list_id=None,
    style=None,
    numbering_type=None,
):
    # NumberingStyleName / ListId are generated per import — assert non-empty
    # and equality across items, never a literal id.
    assert (snap["NumberingStyleName"] or "").strip(), snap
    assert snap["NumberingStyleName"].strip().lower() != "outline", (
        "list item picked up chapter Outline numbering: %s" % snap
    )
    assert (snap["ListId"] or "").strip(), snap
    assert snap["NumberingLevel"] == level, snap
    if label is not None:
        assert snap["ListLabelString"] == label, snap
    if list_id is not None:
        assert snap["ListId"] == list_id, snap
    if style is not None:
        assert snap["NumberingStyleName"] == style, snap
    if numbering_type is not None:
        assert snap["NumberingType"] == numbering_type, snap


@native_test
@with_native_doc("writer")
def test_nested_ol_levels_share_list_id_uno(ctx, doc):
    """3-level nested <ol> becomes real NumberingLevel 0/1/2 on one ListId."""
    _apply_list_fixture(doc, ctx)
    heading = _para(doc, "List numbering boundary")
    assert heading["ParaStyleName"] == "Heading 2", heading
    # Boundary only — do not assert chapter/outline labels.
    _assert_not_list_item(heading, "heading")

    outer_one = _para(doc, "Outer one")
    nested_two = _para(doc, "Nested two")
    nested_three = _para(doc, "Nested three")
    outer_two = _para(doc, "Outer two")

    _assert_list_item(
        outer_one, level=0, label="1.", numbering_type=_NUMBERING_ARABIC
    )
    list_id = outer_one["ListId"]
    style = outer_one["NumberingStyleName"]
    _assert_list_item(
        nested_two,
        level=1,
        label="1.",
        list_id=list_id,
        style=style,
        numbering_type=_NUMBERING_ARABIC,
    )
    _assert_list_item(
        nested_three,
        level=2,
        label="1.",
        list_id=list_id,
        style=style,
        numbering_type=_NUMBERING_ARABIC,
    )
    _assert_list_item(
        outer_two,
        level=0,
        label="2.",
        list_id=list_id,
        style=style,
        numbering_type=_NUMBERING_ARABIC,
    )
    # Default imported list style is per-level 1/2/3, not hierarchical 1.2.3.
    assert nested_two["ListLabelString"] != "1.2", nested_two
    assert nested_three["ListLabelString"] != "1.2.3", nested_three


@native_test
@with_native_doc("writer")
def test_xhtml_export_shows_list_markers_not_hierarchy_uno(ctx, doc):
    """get_document_content XHTML paints Numbering_20_Symbols / Bullet_20_Symbols."""
    tool_ctx = _apply_list_fixture(doc, ctx)
    exported = GetDocumentContent().execute(tool_ctx, scope="full")
    assert exported.get("status") == "ok", exported
    xhtml = exported.get("content") or ""
    numbered = _NUMBERING_SYMBOL_SPAN.findall(xhtml)
    bullets = _BULLET_SYMBOL_SPAN.findall(xhtml)
    assert "1." in numbered, xhtml
    assert "2." in numbered, xhtml
    assert "1.2" not in numbered, numbered
    assert "1.2.3" not in numbered, numbered
    assert any("\u2022" in mark or mark.strip() == "\u2022" for mark in bullets), (
        bullets,
        xhtml,
    )
    assert "Outer one" in xhtml and "Restarted alpha" in xhtml, xhtml


@native_test
@with_native_doc("writer")
def test_search_misses_generated_list_labels_uno(ctx, doc):
    """Known blind spot: XSearchable does not see generated list labels.

    search_in_document uses the document text, not ListLabelString / XHTML
    Numbering_20_Symbols. ``1.`` and hierarchical ``1.2`` therefore return 0
    even though the on-screen / export markers exist. Item text still hits.
    """
    tool_ctx = _apply_list_fixture(doc, ctx)
    search = SearchInDocument()
    generated = search.execute(tool_ctx, pattern="1.")
    hierarchical = search.execute(tool_ctx, pattern="1.2")
    item_text = search.execute(tool_ctx, pattern="Outer one")
    assert generated.get("status") == "ok", generated
    assert generated.get("count") == 0, generated
    assert hierarchical.get("status") == "ok", hierarchical
    assert hierarchical.get("count") == 0, hierarchical
    assert item_text.get("status") == "ok", item_text
    assert item_text.get("count") == 1, item_text


@native_test
@with_native_doc("writer")
def test_replace_item_text_preserves_numbering_uno(ctx, doc):
    """Title-only search replace must keep the item's list level and style."""
    tool_ctx = _apply_list_fixture(doc, ctx)
    before = _para(doc, "Outer one")
    res = ApplyDocumentContent().execute(
        tool_ctx,
        target="search",
        old_content="Outer one",
        content=["Outer renamed"],
    )
    assert res.get("status") == "ok", res
    after = _para(doc, "Outer renamed")
    _assert_list_item(
        after,
        level=before["NumberingLevel"],
        label=before["ListLabelString"],
        list_id=before["ListId"],
        style=before["NumberingStyleName"],
        numbering_type=_NUMBERING_ARABIC,
    )
    nested = _para(doc, "Nested two")
    _assert_list_item(
        nested, level=1, list_id=before["ListId"], style=before["NumberingStyleName"]
    )


@native_test
@with_native_doc("writer")
def test_second_ol_restarts_with_new_list_id_uno(ctx, doc):
    """Body paragraph exits the list; the next <ol> is a new ListId starting at 1."""
    _apply_list_fixture(doc, ctx)
    body = _para(doc, "Body paragraph after nested list.")
    _assert_not_list_item(body, "body after first list")

    first = _para(doc, "Outer one")
    alpha = _para(doc, "Restarted alpha")
    beta = _para(doc, "Restarted beta")
    _assert_list_item(
        alpha, level=0, label="1.", numbering_type=_NUMBERING_ARABIC
    )
    _assert_list_item(
        beta,
        level=0,
        label="2.",
        list_id=alpha["ListId"],
        style=alpha["NumberingStyleName"],
        numbering_type=_NUMBERING_ARABIC,
    )
    assert alpha["ListId"] != first["ListId"], (alpha["ListId"], first["ListId"])
    assert alpha["NumberingStyleName"] != first["NumberingStyleName"], (
        alpha["NumberingStyleName"],
        first["NumberingStyleName"],
    )
    # StarWriter import restarts by starting a new list, not ParaIsNumberingRestart.
    assert alpha["ParaIsNumberingRestart"] is False, alpha


@native_test
@with_native_doc("writer")
def test_mixed_bullet_nested_number_does_not_leak_uno(ctx, doc):
    """Bullet + nested number share a ListId; the following normal para is not a list."""
    _apply_list_fixture(doc, ctx)
    bullet = _para(doc, "Bullet parent")
    nested = _para(doc, "Nested numbered under bullet")
    trailing = _para(doc, "Normal paragraph after mixed list.")

    _assert_list_item(
        bullet,
        level=0,
        label="",
        numbering_type=_NUMBERING_CHAR_SPECIAL,
    )
    _assert_list_item(
        nested,
        level=1,
        label="1.",
        list_id=bullet["ListId"],
        style=bullet["NumberingStyleName"],
        numbering_type=_NUMBERING_ARABIC,
    )
    _assert_not_list_item(trailing, "para after mixed list")

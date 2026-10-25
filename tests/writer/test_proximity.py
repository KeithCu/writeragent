"""Local proximity navigation on Writer documents."""

from unittest.mock import MagicMock

import pytest

from plugin.doc.document_helpers import DocumentService
from plugin.doc.paragraph_search import get_paragraph_ranges
from plugin.framework.errors import DocumentDisposedError, ToolExecutionError
from plugin.tests.testing_utils import ElementStub, WriterDocStub
from plugin.writer.navigation import NavHeading
from plugin.writer.proximity import ProximityService, _collect_anchored
from plugin.writer.tree import TreeService


def _root(*pairs):
    return {
        "children": [
            {"text": text, "para_index": para, "level": 1, "children": []}
            for text, para in pairs
        ]
    }


def _proximity(doc_key="uid:1"):
    services = MagicMock()
    services.document.doc_key.return_value = doc_key
    services.events.subscribe = MagicMock()
    return ProximityService(services)


def test_flatten_tree_links_parents_in_document_order():
    prox = _proximity()
    root = {
        "children": [
            {
                "text": "A",
                "para_index": 0,
                "level": 1,
                "children": [
                    {"text": "A1", "para_index": 1, "level": 2, "children": []},
                    {"text": "A2", "para_index": 2, "level": 2, "children": []},
                ],
            },
            {
                "text": "B",
                "para_index": 3,
                "level": 1,
                "children": [],
            },
        ]
    }
    flat = prox._flatten_tree(root)
    assert [e["node"]["text"] for e in flat] == ["A", "A1", "A2", "B"]
    assert flat[0]["parent"] is None
    assert flat[1]["parent"] is flat[0]
    assert flat[2]["parent"] is flat[0]
    assert flat[3]["parent"] is None


def _nav_stack(elements):
    doc = WriterDocStub(elements)
    doc.CharacterCount = 10
    services = MagicMock()
    doc_svc = DocumentService()
    doc_svc.doc_key = lambda _doc: "uid:edit"
    services.document = doc_svc
    bookmarks = MagicMock()
    bookmarks.ensure_heading_bookmarks.return_value = {}
    bookmarks.get_mcp_bookmark_map.return_value = {}
    services.writer_bookmarks = bookmarks
    services.events.subscribe = MagicMock()
    services.writer_tree = TreeService(services)
    return doc, ProximityService(services)


def test_navigate_heading_text_does_not_start_at_paragraph_zero():
    doc, prox = _nav_stack(
        [
            ElementStub("Preamble"),
            ElementStub("Alpha", outline_level=1),
            ElementStub("body"),
            ElementStub("Beta", outline_level=1),
        ]
    )
    result = prox.navigate_heading(doc, "heading_text:Alpha", "next")
    assert result["heading"]["text"] == "Beta"
    assert result["heading"]["para_index"] == 3


def test_navigate_next_follows_tree_rebuilt_after_character_count_moves():
    elements = [
        ElementStub("Preamble"),
        ElementStub("Alpha", outline_level=1),
        ElementStub("body"),
        ElementStub("Beta", outline_level=1),
    ]
    doc, prox = _nav_stack(elements)
    first = prox.navigate_heading(doc, "heading_text:Alpha", "next")
    assert first["heading"]["text"] == "Beta"

    elements[:] = [
        ElementStub("Alpha", outline_level=1),
        ElementStub("Gamma", outline_level=1),
        ElementStub("Beta", outline_level=1),
    ]
    doc.CharacterCount = 18
    second = prox.navigate_heading(doc, "heading_text:Alpha", "next")
    assert second["heading"]["text"] == "Gamma"
    assert second["heading"]["para_index"] == 1


def test_nav_heading_tool_reports_unresolved_locator():
    doc, prox = _nav_stack([ElementStub("Alpha", outline_level=1)])
    ctx = MagicMock()
    ctx.doc = doc
    ctx.services.writer_proximity = prox
    result = NavHeading().execute(ctx, locator="heading_text:Missing", direction="next")
    assert result["status"] == "error"
    assert "No heading matching 'Missing'" in result["message"]


def test_navigate_missing_bookmark_raises():
    doc, prox = _nav_stack([ElementStub("Alpha", outline_level=1)])

    class _Bookmarks:
        def hasByName(self, _name):
            return False

        def getElementNames(self):
            return ["_mcp_live"]

    doc.getBookmarks = lambda: _Bookmarks()
    with pytest.raises(ToolExecutionError, match="Bookmark '_mcp_stale' not found"):
        prox.navigate_heading(doc, "bookmark:_mcp_stale", "next")


def test_navigate_stale_bookmark_does_not_use_paragraph_zero():
    doc, prox = _nav_stack(
        [
            ElementStub("Alpha", outline_level=1),
            ElementStub("Beta", outline_level=1),
        ]
    )

    class _Anchor:
        def getStart(self):
            raise RuntimeError("stale anchor")

        def getString(self):
            return ""

    class _Mark:
        def getAnchor(self):
            return _Anchor()

    class _Bookmarks:
        def hasByName(self, _name):
            return True

        def getByName(self, _name):
            return _Mark()

        def getElementNames(self):
            return ["_mcp_stale"]

    doc.getBookmarks = lambda: _Bookmarks()
    with pytest.raises(ToolExecutionError, match="Bookmark '_mcp_stale' anchor is not in the document"):
        prox.navigate_heading(doc, "bookmark:_mcp_stale", "next")


def test_navigate_heading_rejects_negative_and_out_of_range_paragraph():
    elements = [
        ElementStub("Preamble"),
        ElementStub("Alpha", outline_level=1),
        ElementStub("body"),
        ElementStub("Beta", outline_level=1),
    ]
    doc, prox = _nav_stack(elements)
    with pytest.raises(ToolExecutionError, match="out of range"):
        prox.navigate_heading(doc, "paragraph:-1", "next")
    with pytest.raises(ToolExecutionError, match="out of range"):
        prox.navigate_heading(doc, "paragraph:99", "next")


def test_surroundings_rejects_negative_and_out_of_range_paragraph():
    elements = [
        ElementStub("Preamble"),
        ElementStub("Alpha", outline_level=1),
        ElementStub("body"),
        ElementStub("Beta", outline_level=1),
    ]
    doc, prox = _nav_stack(elements)
    with pytest.raises(ToolExecutionError, match="out of range"):
        prox.get_surroundings(doc, "paragraph:-1", radius=2, include=["paragraphs"])
    with pytest.raises(ToolExecutionError, match="out of range"):
        prox.get_surroundings(doc, "paragraph:99", radius=2, include=["paragraphs"])


def test_surroundings_radius_zero_returns_only_the_center_paragraph():
    elements = [
        ElementStub("First"),
        ElementStub("Second"),
        ElementStub("Third"),
    ]
    doc, prox = _nav_stack(elements)
    result = prox.get_surroundings(doc, "paragraph:1", radius=0, include=["paragraphs"])
    assert result["range"] == {"start": 1, "end": 1}
    assert len(result["paragraphs"]) == 1
    assert result["paragraphs"][0]["para_index"] == 1


def test_collect_anchored_skips_anchor_not_in_body():
    body_text = object()
    other_text = object()

    anchor_header = MagicMock()
    anchor_header.getText.return_value = other_text
    obj_header = MagicMock()
    obj_header.getAnchor.return_value = anchor_header

    anchor_body = MagicMock()
    anchor_body.getText.return_value = body_text
    obj_body = MagicMock()
    obj_body.getAnchor.return_value = anchor_body

    container = MagicMock()
    container.getElementNames.return_value = ["header_img", "body_img"]
    container.getByName.side_effect = lambda name: obj_header if name == "header_img" else obj_body

    doc_svc = MagicMock()
    doc_svc.find_paragraph_for_range.return_value = 1

    def builder(_obj, name, pi):
        return {"name": name, "pi": pi}

    # Pass confirm_paragraph_index: since anchor.getStart() / para_ranges check might be called
    # Let's see what confirm_paragraph_index does or mock para_ranges appropriately
    # In proximity.py:
    # pi = confirm_paragraph_index(text_obj, anchor, para_ranges, raw_pi)
    # If text_obj is matched and para_ranges has element at 1:
    para_range = MagicMock()
    para_range.getStart.return_value = anchor_body.getStart.return_value
    para_range.getEnd.return_value = anchor_body.getEnd.return_value
    para_ranges = [MagicMock(), para_range]

    items = _collect_anchored(
        container=container,
        doc_svc=doc_svc,
        para_ranges=para_ranges,
        text_obj=body_text,
        entries=None,
        start_idx=0,
        end_idx=2,
        builder=builder,
    )
    assert [item["name"] for item in items] == ["body_img"]


def test_collect_anchored_reraises_disposed_anchor():
    body_text = object()
    obj = MagicMock()
    obj.getAnchor.side_effect = DocumentDisposedError("disposed")

    container = MagicMock()
    container.getElementNames.return_value = ["img"]
    container.getByName.return_value = obj

    doc_svc = MagicMock()

    with pytest.raises(DocumentDisposedError):
        _collect_anchored(
            container=container,
            doc_svc=doc_svc,
            para_ranges=[],
            text_obj=body_text,
            entries=None,
            start_idx=0,
            end_idx=2,
            builder=lambda _o, n, pi: {"name": n},
        )


def test_surroundings_keeps_comments_after_one_unreadable_annotation():
    doc = WriterDocStub([ElementStub("Para0"), ElementStub("Para1")])
    body_text = doc.getText()
    doc.getText = lambda: body_text

    # First annotation raises RuntimeError on getPropertyValue("Content")
    bad_field = MagicMock()
    bad_field.supportsService.return_value = True
    bad_anchor = MagicMock()
    bad_anchor.getText.return_value = body_text
    bad_field.getAnchor.return_value = bad_anchor

    def bad_get_prop(prop):
        if prop == "Content":
            raise RuntimeError("unreadable content")
        if prop == "Author":
            return "Bad Author"
        if prop == "Resolved":
            return False
        return None

    bad_field.getPropertyValue.side_effect = bad_get_prop

    # Second annotation is readable
    good_field = MagicMock()
    good_field.supportsService.return_value = True
    good_anchor = MagicMock()
    good_anchor.getText.return_value = body_text
    good_field.getAnchor.return_value = good_anchor

    def good_get_prop(prop):
        if prop == "Content":
            return "Good comment content"
        if prop == "Author":
            return "Good Author"
        if prop == "Resolved":
            return True
        return None

    good_field.getPropertyValue.side_effect = good_get_prop

    class FieldEnum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self) -> bool:
            return len(self._items) > 0

        def nextElement(self):
            return self._items.pop(0)

    fields_obj = MagicMock()
    fields_obj.createEnumeration.return_value = FieldEnum([bad_field, good_field])
    doc.getTextFields = lambda: fields_obj

    services = MagicMock()
    doc_svc = MagicMock()
    doc_svc.resolve_locator.return_value = {"para_index": 0}
    doc_svc.get_paragraph_ranges.side_effect = get_paragraph_ranges
    doc_svc.find_paragraph_for_range.return_value = 1
    services.document = doc_svc
    prox = ProximityService(services)

    result = prox.get_surroundings(doc, "paragraph:0", radius=5, include=["comments"])
    assert len(result["comments"]) == 1
    c = result["comments"][0]
    assert c["author"] == "Good Author"
    assert c["content"] == "Good comment content"
    assert c["paragraph_index"] == 1
    assert c["resolved"] is True


def test_surroundings_reraises_disposed_annotation():
    doc = WriterDocStub([ElementStub("Para0"), ElementStub("Para1")])
    body_text = doc.getText()
    doc.getText = lambda: body_text

    disposed_field = MagicMock()
    disposed_field.supportsService.return_value = True
    anchor = MagicMock()
    anchor.getText.return_value = body_text
    disposed_field.getAnchor.return_value = anchor
    disposed_field.getPropertyValue.side_effect = DocumentDisposedError("disposed field")

    class FieldEnum:
        def __init__(self, items):
            self._items = list(items)

        def hasMoreElements(self) -> bool:
            return len(self._items) > 0

        def nextElement(self):
            return self._items.pop(0)

    fields_obj = MagicMock()
    fields_obj.createEnumeration.return_value = FieldEnum([disposed_field])
    doc.getTextFields = lambda: fields_obj

    services = MagicMock()
    doc_svc = MagicMock()
    doc_svc.resolve_locator.return_value = {"para_index": 0}
    doc_svc.get_paragraph_ranges.side_effect = get_paragraph_ranges
    doc_svc.find_paragraph_for_range.return_value = 1
    services.document = doc_svc
    prox = ProximityService(services)

    with pytest.raises(DocumentDisposedError):
        prox.get_surroundings(doc, "paragraph:0", radius=5, include=["comments"])


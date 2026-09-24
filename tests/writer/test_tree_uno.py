from types import SimpleNamespace

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("writer")
def test_tree_service_basic(ctx, doc):
    from plugin.writer.tree import TreeService
    from plugin.writer.specialized.bookmarks import BookmarkService
    from plugin.framework.event_bus import EventBus
    from plugin.doc.document_helpers import DocumentService
    
    # Setup doc content with headings
    text = doc.getText()
    cursor = text.createTextCursor()

    # H1
    text.insertString(cursor, "H1", False)
    cursor.setPropertyValue("ParaStyleName", "Heading 1")
    text.insertControlCharacter(cursor, 0, False)

    # P1
    text.insertString(cursor, "P1", False)
    text.insertControlCharacter(cursor, 0, False)

    # H1.1
    text.insertString(cursor, "H1.1", False)
    cursor.setPropertyValue("ParaStyleName", "Heading 2")
    text.insertControlCharacter(cursor, 0, False)

    events = EventBus()
    doc_svc = DocumentService()
    services = SimpleNamespace()
    services.document = doc_svc
    services.events = events
    services.writer_bookmarks = BookmarkService()
    services.writer_tree = TreeService(services)
    tree_svc = services.writer_tree

    # 1. Test build_heading_tree from TreeService natively
    tree = tree_svc.build_heading_tree(doc)
    assert tree is not None, "TreeService.build_heading_tree returned None"
    assert "children" in tree and len(tree["children"]) >= 1

    h1 = tree["children"][0]
    assert h1["text"] == "H1", "First child should be H1"

    # 2. Test resolve_writer_locator from TreeService natively
    res = tree_svc.resolve_writer_locator(doc, "heading", "1.1")
    assert res is not None and res.get("para_index") == 2, f"Failed to resolve heading:1.1, got {res}"

    res = tree_svc.resolve_writer_locator(doc, "heading_text", "H1.1")
    assert res is not None and res.get("para_index") == 2, f"Failed to resolve heading_text:H1.1, got {res}"


def _heading_texts(tree):
    return [child["text"] for child in tree.get("children", [])]


def _tree_svc_on_bus():
    from types import SimpleNamespace

    from plugin.doc.document_helpers import DocumentService
    from plugin.framework.event_bus import get_event_bus
    from plugin.writer.specialized.bookmarks import BookmarkService
    from plugin.writer.tree import TreeService

    events = get_event_bus()
    services = SimpleNamespace()
    services.document = DocumentService()
    services.events = events
    services.writer_bookmarks = BookmarkService()
    tree_svc = TreeService(services)
    return tree_svc, events


def _insert_heading(doc, text, style="Heading 1"):
    cursor = doc.getText().createTextCursor()
    cursor.gotoEnd(False)
    body = doc.getText()
    body.insertControlCharacter(cursor, 0, False)
    body.insertString(cursor, text, False)
    cursor.setPropertyValue("ParaStyleName", style)


def _drain_ui(ctx):
    from plugin.framework.uno_context import get_toolkit

    toolkit = get_toolkit(ctx)
    if toolkit is not None:
        toolkit.processEventsToIdle()


@native_test
@with_native_doc("writer")
def test_tree_cache_two_wrappers_one_entry(ctx, doc):
    from plugin.framework.uno_context import get_runtime_uid

    _insert_heading(doc, "Shared")
    other = doc.getCurrentController().getModel()
    assert get_runtime_uid(doc) == get_runtime_uid(other)
    assert get_runtime_uid(doc)

    tree_svc, events = _tree_svc_on_bus()
    try:
        key_a = tree_svc._doc_svc.doc_key(doc)
        key_b = tree_svc._doc_svc.doc_key(other)
        assert key_a == key_b
        tree_a = tree_svc.build_heading_tree(doc)
        tree_b = tree_svc.build_heading_tree(other)
        assert tree_a is tree_b
        assert len(tree_svc._tree_cache) == 1
        assert "Shared" in _heading_texts(tree_a)
    finally:
        events.unsubscribe("document:cache_invalidated", tree_svc._on_cache_invalidated)


@native_test
@with_native_doc("writer")
def test_tree_cache_external_edit_invalidates(ctx, doc):
    _insert_heading(doc, "Before")
    tree_svc, events = _tree_svc_on_bus()
    try:
        first = tree_svc.build_heading_tree(doc)
        assert "Before" in _heading_texts(first)
        assert "After" not in _heading_texts(first)
        cached = tree_svc.build_heading_tree(doc)
        assert cached is first

        _insert_heading(doc, "After")
        _drain_ui(ctx)

        second = tree_svc.build_heading_tree(doc)
        assert second is not first
        assert "After" in _heading_texts(second)
    finally:
        events.unsubscribe("document:cache_invalidated", tree_svc._on_cache_invalidated)


@native_test
def test_tree_cache_two_docs_do_not_share_entry(ctx):
    from plugin.tests.testing_utils import TestingFactory

    tree_svc, events = _tree_svc_on_bus()
    try:
        with TestingFactory.native_doc(ctx, doc_type="writer", hidden=True, reuse=False) as doc_a:
            with TestingFactory.native_doc(ctx, doc_type="writer", hidden=True, reuse=False) as doc_b:
                _insert_heading(doc_a, "DocA")
                _insert_heading(doc_b, "DocB")
                tree_a = tree_svc.build_heading_tree(doc_a)
                tree_b = tree_svc.build_heading_tree(doc_b)
                assert tree_a is not tree_b
                assert "DocA" in _heading_texts(tree_a)
                assert "DocB" not in _heading_texts(tree_a)
                assert "DocB" in _heading_texts(tree_b)
                assert "DocA" not in _heading_texts(tree_b)
                assert tree_svc._doc_svc.doc_key(doc_a) != tree_svc._doc_svc.doc_key(doc_b)
    finally:
        events.unsubscribe("document:cache_invalidated", tree_svc._on_cache_invalidated)


def _flatten_heading_nodes(node):
    out = []
    for child in node.get("children", []):
        out.append(child)
        out.extend(_flatten_heading_nodes(child))
    return out


def _heading_by_text(tree):
    return {n["text"]: n for n in _flatten_heading_nodes(tree)}


@native_test
@with_native_doc("writer", reuse=False)
def test_chapter_number_on_off_and_locator_uno(ctx, doc):
    """Discussion #876: ListLabelString → optional chapter_number + locator."""
    from plugin.doc.document_helpers import resolve_locator
    from plugin.framework.errors import ToolExecutionError
    from tests.writer.chapter_numbering_fixtures import (
        EXPECTED_LABELS_SUFFIX_EMPTY,
        disable_chapter_numbering,
        enable_chapter_numbering,
        insert_chapter_heading_fixture,
    )

    insert_chapter_heading_fixture(doc)
    tree_svc, events = _tree_svc_on_bus()
    try:
        disable_chapter_numbering(doc)
        off = _heading_by_text(tree_svc.build_heading_tree(doc))
        assert off, off
        for node in off.values():
            assert "chapter_number" not in node, node
            # Paint label is not written into heading body text.
            assert not str(node["text"]).startswith(("1", "2", "3")), node

        try:
            resolve_locator(doc, "chapter_number:3.1")
            raise AssertionError("chapter_number: must error when numbering is off")
        except ToolExecutionError as exc:
            msg = str(exc)
            assert "chapter_number:3.1" in msg
            assert "off" in msg.lower() or "omit" in msg.lower()

        try:
            tree_svc.resolve_writer_locator(doc, "chapter_number", "1.1")
            raise AssertionError("TreeService locator must error when off")
        except ToolExecutionError as exc:
            assert "chapter_number:1.1" in str(exc)

        enable_chapter_numbering(doc, suffix="")
        tree_svc._drop_tree_cache()
        on = _heading_by_text(tree_svc.build_heading_tree(doc))
        from plugin.doc.text_helpers import build_heading_tree as light_build_heading_tree

        light = _heading_by_text(light_build_heading_tree(doc))
        for title, expected in EXPECTED_LABELS_SUFFIX_EMPTY.items():
            assert on[title]["chapter_number"] == expected, (title, on[title])
            assert on[title]["text"] == title
            assert light[title]["chapter_number"] == expected, (title, light[title])
            assert light[title]["text"] == title

        # Ordinal heading: is sibling path, not the chapter label.
        ordinal = tree_svc.resolve_writer_locator(doc, "heading", "1.2")
        assert on["Section Beta"]["para_index"] == ordinal["para_index"]

        enable_chapter_numbering(doc, suffix="", start_with=3)
        tree_svc._drop_tree_cache()
        started = _heading_by_text(tree_svc.build_heading_tree(doc))
        assert started["Chapter One"]["chapter_number"] == "3"
        assert started["Section Alpha"]["chapter_number"] == "3.1"
        assert started["Section Alpha"]["text"] == "Section Alpha"
        hit = resolve_locator(doc, "chapter_number:3.1")
        assert hit["para_index"] == started["Section Alpha"]["para_index"]
        hit_dot = tree_svc.resolve_writer_locator(doc, "chapter_number", "3.1.")
        assert hit_dot["para_index"] == started["Section Alpha"]["para_index"]
        # start_with=3: ordinal 1.1 is still Section Alpha, but chapter_number:1.1 is missing.
        still_ordinal = tree_svc.resolve_writer_locator(doc, "heading", "1.1")
        assert still_ordinal["para_index"] == started["Section Alpha"]["para_index"]
        try:
            resolve_locator(doc, "chapter_number:1.1")
            raise AssertionError("must not invent chapter_number from ordinals")
        except ToolExecutionError as exc:
            assert "chapter_number:1.1" in str(exc)

        enable_chapter_numbering(doc, suffix=".")
        tree_svc._drop_tree_cache()
        dotted = _heading_by_text(tree_svc.build_heading_tree(doc))
        assert dotted["Section Alpha"]["chapter_number"] == "1.1"
        assert resolve_locator(doc, "chapter_number:1.1")["para_index"] == dotted["Section Alpha"]["para_index"]

        serialized = tree_svc.get_document_tree(doc, content_strategy="heading_only", depth=0)
        ser_map = _heading_by_text({"children": serialized["children"]})
        assert ser_map["Section Alpha"]["chapter_number"] == "1.1"
        kids = tree_svc.get_heading_children(doc, locator="chapter_number:1", content_strategy="heading_only", depth=1)
        assert kids["parent"]["chapter_number"] == "1"
        child_nums = [c.get("chapter_number") for c in kids["children"] if c.get("type") == "heading"]
        assert "1.1" in child_nums
    finally:
        events.unsubscribe("document:cache_invalidated", tree_svc._on_cache_invalidated)


@native_test
@with_native_doc("writer", reuse=False)
def test_chapter_number_not_invented_from_literal_title_uno(ctx, doc):
    """A title like DOCUMENT 7 is not a chapter_number — only ListLabelString is."""
    from tests.writer.chapter_numbering_fixtures import (
        disable_chapter_numbering,
        enable_chapter_numbering,
        insert_chapter_heading_fixture,
    )

    insert_chapter_heading_fixture(
        doc,
        rows=(
            ("Heading 1", "DOCUMENT 7: Vulnerability"),
            ("Heading 2", "Geographical situation"),
        ),
    )
    tree_svc, events = _tree_svc_on_bus()
    try:
        disable_chapter_numbering(doc)
        off = _heading_by_text(tree_svc.build_heading_tree(doc))
        lit = off["DOCUMENT 7: Vulnerability"]
        assert "chapter_number" not in lit
        assert lit["text"] == "DOCUMENT 7: Vulnerability"

        enable_chapter_numbering(doc, suffix="", start_with=3)
        tree_svc._drop_tree_cache()
        on = _heading_by_text(tree_svc.build_heading_tree(doc))
        assert on["DOCUMENT 7: Vulnerability"]["chapter_number"] == "3"
        assert on["DOCUMENT 7: Vulnerability"]["text"] == "DOCUMENT 7: Vulnerability"
        assert on["Geographical situation"]["chapter_number"] == "3.1"
        assert "7" not in {n.get("chapter_number") for n in on.values()}
    finally:
        events.unsubscribe("document:cache_invalidated", tree_svc._on_cache_invalidated)

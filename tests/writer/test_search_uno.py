from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc
from plugin.writer.search import SearchInDocument


def _populate_search_doc(doc):
    text = doc.getText()
    cursor = text.createTextCursor()

    # 0: Heading
    text.insertString(cursor, "Introduction to testing", False)
    cursor.setPropertyValue("ParaStyleName", "Heading 1")
    text.insertControlCharacter(cursor, 0, False)

    # 1: Paragraph
    text.insertString(cursor, "This is the first paragraph. We will find this needle in a haystack.", False)
    cursor.setPropertyValue("ParaStyleName", "Standard")
    text.insertControlCharacter(cursor, 0, False)

    # 2: Paragraph
    text.insertString(cursor, "Another paragraph. Needles are sharp. We also have some testing data here.", False)
    text.insertControlCharacter(cursor, 0, False)


class MockContext:
    def __init__(self, doc, ctx):
        self.doc = doc
        self.ctx = ctx
        self.services = MockServices(doc)


class MockWriterIndexService:
    def search_boolean(self, doc, query, max_results=20, context_paragraphs=1):
        if "error" in query:
            raise ValueError("Test error from search_boolean")
        return {
            "matches": [{"paragraph_index": 1, "text": "This is the first paragraph", "context": []}],
            "count": 1
        }
    def get_index_stats(self, doc):
        return {"stems": 100, "paragraphs": 3}


class MockServices:
    def __init__(self, doc):
        from plugin.doc.document_helpers import DocumentService
        from plugin.framework.event_bus import EventBus
        self.events = EventBus()
        self.document = DocumentService()
        self.writer_index = MockWriterIndexService()


@native_test
@with_native_doc("writer")
def test_search_in_document_basic(ctx, doc):
    _populate_search_doc(doc)
    tool = SearchInDocument()
    mock_ctx = MockContext(doc, ctx)

    # Simple search
    res = tool.execute(mock_ctx, pattern="needle")
    assert res["status"] == "ok"
    assert res["count"] == 2
    assert len(res["matches"]) == 2

    match1 = res["matches"][0]
    assert match1["text"] == "needle"
    assert match1["location"] == "body"
    assert "needle" in match1["context"].lower()

    match2 = res["matches"][1]
    assert match2["text"] == "Needle"
    assert match2["location"] == "body"
    assert "Needle" in match2["context"]


@native_test
@with_native_doc("writer")
def test_search_in_document_case_sensitive(ctx, doc):
    _populate_search_doc(doc)
    tool = SearchInDocument()
    mock_ctx = MockContext(doc, ctx)

    res = tool.execute(mock_ctx, pattern="Needle", case_sensitive=True)
    assert res["status"] == "ok"
    assert res["count"] == 1
    assert res["matches"][0]["location"] == "body"
    assert res["matches"][0]["text"] == "Needle"


@native_test
@with_native_doc("writer")
def test_search_in_document_regex(ctx, doc):
    _populate_search_doc(doc)
    tool = SearchInDocument()
    mock_ctx = MockContext(doc, ctx)

    res = tool.execute(mock_ctx, pattern=r"Needles? are \w+", regex=True)
    assert res["status"] == "ok"
    assert res["count"] == 1
    assert res["matches"][0]["text"] == "Needles are sharp"


@native_test
@with_native_doc("writer")
def test_find_first_range_chains_across_paragraphs(ctx, doc):
    from plugin.writer.search import find_first_range
    text = doc.getText()
    cursor = text.createTextCursor()

    # Setup document with surrounding text
    cursor.setString("Prefix text Line 1 match")
    cursor.gotoEnd(False)
    text.insertControlCharacter(cursor, 0, False)
    cursor.gotoEnd(False)

    cursor.setString("Line 2 match More text")
    cursor.gotoEnd(False)

    search_string = "Line 1 match\nLine 2 match"
    rng = find_first_range(doc, search_string)

    assert rng is not None
    # getString() joins paragraphs with the platform newline: "\r\n" on Windows.
    matched_text = rng.getString().replace("\r\n", "\n")

    assert matched_text == search_string, "Matched text included extra paragraph content!"


@native_test
def test_advanced_search_tool():
    return


@native_test
def test_get_index_stats():
    return


@native_test
@with_native_doc("writer")
def test_search_in_document_zero_width_regex_is_truncated(ctx, doc):
    text = doc.getText()
    cursor = text.createTextCursor()
    cursor.setString("Hello")
    tool = SearchInDocument()
    mock_ctx = MockContext(doc, ctx)

    # In this LibreOffice environment, ICU findFirst for "^" returns None (0 hits)
    # rather than an empty TextRange hit. The zero-width contract is verified in
    # unit tests (test_search_in_document_zero_width_sets_truncated).
    sd = doc.createSearchDescriptor()
    sd.SearchString = "^"
    sd.SearchRegularExpression = True
    found = doc.findFirst(sd)
    assert found is None

    res = tool.execute(mock_ctx, pattern="^", regex=True)
    assert res["status"] == "ok"
    assert res["count"] == 0
    assert res["matches"] == []


@native_test
@with_native_doc("writer")
def test_search_in_document_icu_only_regex_uno(ctx, doc):
    _populate_search_doc(doc)
    tool = SearchInDocument()
    mock_ctx = MockContext(doc, ctx)
    res = tool.execute(mock_ctx, pattern=r"\p{Lu}\w+", regex=True)
    assert res["status"] == "ok"
    assert res["count"] >= 2
    assert res["matches"][0]["text"].startswith("Introduction")


@native_test
@with_native_doc("writer")
def test_search_in_document_python_only_regex_is_invalid_regex_uno(ctx, doc):
    _populate_search_doc(doc)
    tool = SearchInDocument()
    mock_ctx = MockContext(doc, ctx)
    res = tool.execute(mock_ctx, pattern=r"(?P<w>Needles)", regex=True)
    assert res["status"] == "error"
    assert res["code"] == "INVALID_REGEX"
    assert "(?<name>...)" in res["message"]
    assert res.get("count", res.get("details", {}).get("count")) == 0


@native_test
@with_native_doc("writer")
def test_search_in_document_plain_bad_regex_is_invalid_regex_uno(ctx, doc):
    _populate_search_doc(doc)
    tool = SearchInDocument()
    mock_ctx = MockContext(doc, ctx)
    res = tool.execute(mock_ctx, pattern="([a-", regex=True)
    assert res["status"] == "error"
    assert res["code"] == "INVALID_REGEX"


@native_test
@with_native_doc("writer")
def test_tracked_deletions_hidden_from_reads_and_searches(ctx, doc):
    """Pending tracked deletions spanning multiple paragraphs must be hidden from
    get_string_without_tracked_deletions, range reads, full export, get_document_content,
    search_in_document, and find_text_ranges offsets. get_document_content must list the
    deleted text only under tracked_changes and leave the live document untouched."""
    from plugin.doc.text_helpers import get_string_without_tracked_deletions
    from plugin.tests.testing_utils import TestingFactory
    from plugin.writer.get_document_content import GetDocumentContent
    from plugin.writer.html_export import document_to_content
    from plugin.writer.search import SearchInDocument, find_text_ranges

    doc.setPropertyValue("RecordChanges", False)
    text = doc.getText()
    cursor = text.createTextCursor()

    # Insert P1 to P4
    text.insertString(cursor, "P1 start. To be deleted in P1.", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "P2 entire paragraph deleted.", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "P3 deleted start. P3 live end.", False)
    text.insertControlCharacter(cursor, 0, False)
    text.insertString(cursor, "P4 delta live text.", False)

    # Turn on RecordChanges and delete from middle of P1 to middle of P3
    doc.setPropertyValue("RecordChanges", True)
    try:
        sd = doc.createSearchDescriptor()
        sd.SearchString = "To be deleted in P1."
        hit1 = doc.findFirst(sd)
        assert hit1 is not None

        sd.SearchString = "P3 deleted start. "
        hit2 = doc.findFirst(sd)
        assert hit2 is not None

        del_cursor = text.createTextCursorByRange(hit1.getStart())
        del_cursor.gotoRange(hit2.getEnd(), True)
        del_cursor.setString("")

        show_changes = doc.getPropertyValue("ShowChanges")
        modified = doc.isModified()
        redline_count = doc.getRedlines().getCount()
        assert redline_count >= 1

        # 1. get_string_without_tracked_deletions on body
        visible_text = get_string_without_tracked_deletions(text)
        assert "To be deleted in P1." not in visible_text
        assert "P2 entire paragraph deleted." not in visible_text
        assert "P3 deleted start." not in visible_text
        assert "P1 start." in visible_text
        assert "P3 live end." in visible_text
        assert "P4 delta live text." in visible_text

        # 2. scope=range read covering the visible text
        range_read = document_to_content(doc, ctx, scope="range", range_start=0, range_end=len(visible_text))
        assert "To be deleted in P1." not in range_read
        assert "P2 entire paragraph deleted." not in range_read
        assert "P3 deleted start." not in range_read
        assert "P1 start." in range_read
        assert "P3 live end." in range_read
        assert "P4 delta live text." in range_read

        # 3. full document_to_content read: does not contain deleted text and doc is unmodified
        full_read = document_to_content(doc, ctx, scope="full")
        assert "To be deleted in P1." not in full_read
        assert "P2 entire paragraph deleted." not in full_read
        assert "P3 deleted start." not in full_read
        assert "P1 start." in full_read
        assert "P3 live end." in full_read
        assert "P4 delta live text." in full_read
        assert doc.getPropertyValue("RecordChanges") is True
        assert doc.getPropertyValue("ShowChanges") == show_changes
        assert doc.isModified() == modified
        assert doc.getRedlines().getCount() == redline_count
        assert "P2 entire paragraph deleted." in text.getString()

        # 3b. get_document_content: content is the accept view; deleted text is only
        # in tracked_changes; the live document is untouched.
        tool_ctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native")
        tool_res = GetDocumentContent().execute(tool_ctx, scope="full")
        assert tool_res["status"] == "ok", tool_res
        tool_html = tool_res.get("content") or ""
        assert "To be deleted in P1." not in tool_html
        assert "P2 entire paragraph deleted." not in tool_html
        assert "P3 deleted start." not in tool_html
        assert "P1 start." in tool_html
        assert "P3 live end." in tool_html
        assert "P4 delta live text." in tool_html
        changes = tool_res.get("tracked_changes") or []
        deletion_text = " ".join(
            item.get("text", "") for item in changes if item.get("type") == "deletion"
        )
        assert "To be deleted in P1." in deletion_text, changes
        assert "P2 entire paragraph deleted." in deletion_text, changes
        assert "P3 deleted start." in deletion_text, changes
        assert tool_res.get("tracked_changes_total", 0) >= 1
        assert doc.getPropertyValue("RecordChanges") is True
        assert doc.getPropertyValue("ShowChanges") == show_changes
        assert doc.isModified() == modified
        assert doc.getRedlines().getCount() == redline_count
        assert "P2 entire paragraph deleted." in text.getString()

        # 4. search_in_document for a word inside the deletion returns 0 hits,
        # while a live word hits as expected
        mock_ctx = MockContext(doc, ctx)
        search_res = SearchInDocument().execute(mock_ctx, pattern="entire paragraph")
        assert search_res["status"] == "ok"
        assert search_res["count"] == 0
        assert search_res["matches"] == []

        live_res = SearchInDocument().execute(mock_ctx, pattern="live end")
        assert live_res["status"] == "ok"
        assert live_res["count"] == 1
        assert len(live_res["matches"]) == 1

        # 5. return_offsets for a word after the deletion matches the range-read offset
        target_word = "delta"
        ranges = find_text_ranges(doc, target_word)
        assert len(ranges) == 1
        delta_offset = ranges[0]["start"]
        assert delta_offset == visible_text.index(target_word)
        delta_slice = document_to_content(
            doc, ctx, scope="range", range_start=delta_offset, range_end=delta_offset + len(target_word)
        )
        assert target_word in delta_slice
    finally:
        doc.setPropertyValue("RecordChanges", False)

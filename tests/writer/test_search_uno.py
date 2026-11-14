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


class MockServices:
    def __init__(self, doc):
        from plugin.doc.document_helpers import DocumentService
        from plugin.framework.event_bus import EventBus
        self.events = EventBus()
        self.document = DocumentService()


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

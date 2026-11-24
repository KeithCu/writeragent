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
def test_search_in_document_zero_width_regex_finds_nothing_uno(ctx, doc):
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
def test_search_miss_names_closest_header_text_uno(ctx, doc):
    from plugin.tests.testing_utils import native_writer_tool_context
    from plugin.writer import search as search_mod
    from plugin.writer.apply_document_content import ApplyDocumentContent

    doc.getText().setString("Body paragraph one.")
    page_styles = doc.getStyleFamilies().getByName("PageStyles")
    standard = page_styles.getByName("Standard")
    standard.setPropertyValue("HeaderIsOn", True)
    standard.getPropertyValue("HeaderText").setString("Acme Corp Quarterly Report")

    res = ApplyDocumentContent().execute(
        native_writer_tool_context(doc, ctx),
        target="search",
        old_content="Acme Corp Quartrely Report",
        content="X",
    )
    assert res.get("status") == "error", res
    msg = str(res.get("message", ""))
    assert "differs at character 15: r vs e" in msg, msg
    assert "Acme Corp Quarte…" in msg, msg
    assert search_mod.get_search_miss_haystack(doc).count("Acme Corp Quarterly Report") == 1


@native_test
@with_native_doc("writer")
def test_replace_all_defined_term_exact_case_uno(ctx, doc):
    from plugin.tests.testing_utils import native_writer_tool_context
    from plugin.writer.apply_document_content import ApplyDocumentContent

    doc.getText().setString("The Seller and the seller.")
    res = ApplyDocumentContent().execute(
        native_writer_tool_context(doc, ctx),
        target="search",
        old_content="Seller",
        content="Vendor",
        all_matches=True,
    )
    assert res.get("status") == "ok", res
    assert res.get("replaced_count") == 1
    assert res.get("matched_case_sensitive") is True
    assert doc.getText().getString() == "The Vendor and the seller."


@native_test
@with_native_doc("writer")
def test_replace_all_en_dash_keeps_hyphens_uno(ctx, doc):
    from plugin.tests.testing_utils import native_writer_tool_context
    from plugin.writer.apply_document_content import ApplyDocumentContent

    doc.getText().setString("e-mail 2020\u20132021")
    res = ApplyDocumentContent().execute(
        native_writer_tool_context(doc, ctx),
        target="search",
        old_content="\u2013",
        content=" to ",
        all_matches=True,
    )
    assert res.get("status") == "ok", res
    assert res.get("replaced_count") == 1
    assert res.get("matched_folded") is False
    assert doc.getText().getString() == "e-mail 2020 to 2021"


@native_test
@with_native_doc("writer")
def test_two_paragraph_needle_prefers_chain_uno(ctx, doc):
    from plugin.tests.testing_utils import native_writer_tool_context
    from plugin.writer.apply_document_content import ApplyDocumentContent

    doc.getText().setString("Intro: Revenue Growth was strong.\rRevenue\rGrowth")
    res = ApplyDocumentContent().execute(
        native_writer_tool_context(doc, ctx),
        target="search",
        old_content="Revenue\nGrowth",
        content="Summary",
    )
    assert res.get("status") == "ok", res
    body = doc.getText().getString().replace("\r\n", "\n").replace("\r", "\n")
    assert body.startswith("Intro: Revenue Growth was strong.")
    assert "Summary" in body


@native_test
@with_native_doc("writer")
def test_explicit_case_sensitive_chains_paragraphs_uno(ctx, doc):
    from plugin.tests.testing_utils import native_writer_tool_context
    from plugin.writer.apply_document_content import ApplyDocumentContent

    doc.getText().setString("Intro: Revenue Growth was strong.\rRevenue\rGrowth")
    tool = ApplyDocumentContent()
    tctx = native_writer_tool_context(doc, ctx)

    hit = tool.execute(
        tctx,
        target="search",
        old_content="Revenue\nGrowth",
        content="Summary",
        case_sensitive=True,
        dry_run=True,
    )
    assert hit.get("status") == "ok", hit
    assert hit.get("replaceable_count") == 1

    miss = tool.execute(
        tctx,
        target="search",
        old_content="Revenue\ngrowth",
        content="Summary",
        case_sensitive=True,
        dry_run=True,
    )
    assert miss.get("status") == "ok", miss
    assert miss.get("replaceable_count") == 0


@native_test
@with_native_doc("writer", reuse=False)
def test_search_miss_names_closest_text_box_text_uno(ctx, doc):
    from com.sun.star.awt import Point, Size
    from plugin.tests.testing_utils import native_writer_tool_context
    from plugin.writer.apply_document_content import ApplyDocumentContent
    from plugin.writer.search import get_search_miss_haystack

    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Body paragraph.", False)

    shape = doc.createInstance("com.sun.star.drawing.RectangleShape")
    shape.setName("HintBox")
    shape.setSize(Size(4000, 2000))
    shape.setPosition(Point(1000, 1000))
    text.insertTextContent(cur, shape, False)
    shape.setPropertyValue("TextBox", True)
    box_phrase = "atchar box unique text"
    shape.setString(box_phrase)

    hay = get_search_miss_haystack(doc)
    assert hay.count(box_phrase) == 1, hay

    res = ApplyDocumentContent().execute(
        native_writer_tool_context(doc, ctx),
        target="search",
        old_content="atchar box unique texe",
        content="updated",
    )
    assert res.get("status") == "error", res
    msg = res.get("message", "")
    assert "closest match" in msg, res
    assert "atchar box" in msg, res


@native_test
@with_native_doc("writer")
def test_search_skips_tracked_deleted_text_and_table_cell_uno(ctx, doc):
    from plugin.tests.testing_utils import native_writer_tool_context, strike_across_paragraphs, strike_across_table
    from plugin.writer.search import SearchInDocument

    text = doc.getText()
    strike_across_paragraphs(doc, text)

    tctx = native_writer_tool_context(doc, ctx)
    assert SearchInDocument().execute(tctx, pattern="gone")["count"] == 0
    tail_res = SearchInDocument().execute(tctx, pattern="Tail")
    assert tail_res["count"] == 1
    assert "gone" not in tail_res["matches"][0]["context"]

    # Table struck by a body redline: hit in its cell is skipped.
    strike_across_table(doc, text)
    assert SearchInDocument().execute(tctx, pattern="CellGone")["count"] == 0
    assert SearchInDocument().execute(tctx, pattern="Before")["count"] == 1


@native_test
@with_native_doc("writer")
def test_apply_content_multi_paragraph_does_not_match_across_table_uno(ctx, doc):
    from plugin.tests.testing_utils import native_writer_tool_context
    from plugin.writer.apply_document_content import ApplyDocumentContent

    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Alpha", False)
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    text.insertTextContent(text.getEnd(), tbl, False)
    tbl.getCellByName("A1").setString("Cell1")
    cur.gotoEnd(False)
    text.insertString(cur, "Beta", False)

    tctx = native_writer_tool_context(doc, ctx)
    res = ApplyDocumentContent().execute(
        tctx, target="search", old_content="Alpha\nBeta", content="Alpha\nGamma"
    )
    assert res["status"] == "error"
    assert res["code"] == "OLD_CONTENT_NOT_FOUND"
    assert doc.getTextTables().getCount() == 1
    assert tbl.getCellByName("A1").getString() == "Cell1"
    from plugin.doc.text_helpers import iter_elements

    paras = [
        el.getString()
        for el in iter_elements(text.createEnumeration())
        if el.supportsService("com.sun.star.text.Paragraph")
    ]
    assert paras == ["Alpha", "Beta"]


@native_test
@with_native_doc("writer")
def test_apply_content_multi_paragraph_inside_one_cell_matches_uno(ctx, doc):
    from plugin.tests.testing_utils import native_writer_tool_context
    from plugin.writer.apply_document_content import ApplyDocumentContent

    text = doc.getText()
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    text.insertTextContent(text.getEnd(), tbl, False)
    cell = tbl.getCellByName("A1")
    c_cur = cell.createTextCursor()
    cell.insertString(c_cur, "One", False)
    cell.insertControlCharacter(c_cur, 0, False)
    cell.insertString(c_cur, "Two", False)

    tctx = native_writer_tool_context(doc, ctx)
    res = ApplyDocumentContent().execute(
        tctx, target="search", old_content="One\nTwo", content="One\nThree"
    )
    assert res["status"] == "ok", res
    assert "Three" in cell.getString()

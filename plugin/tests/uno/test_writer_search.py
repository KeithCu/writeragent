try:
    import uno
    import pytest
except ImportError:
    pass

try:
    from plugin.testing_runner import setup, teardown, native_test
    from plugin.framework.uno_context import get_desktop
    from plugin.modules.writer.search import SearchInDocument, GetIndexStats
except ImportError:
    setup, teardown, native_test = (lambda f: f), (lambda f: f), (lambda f: f)

_test_doc = None
_test_ctx = None

@setup
def setup_search_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx

    desktop = get_desktop(ctx)
    from com.sun.star.beans import PropertyValue
    hidden_prop = PropertyValue()
    hidden_prop.Name = "Hidden"
    hidden_prop.Value = True
    _test_doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, (hidden_prop,))

    text = _test_doc.getText()
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

@teardown
def teardown_search_tests():
    if _test_doc:
        _test_doc.close(True)

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
        from plugin.framework.document import DocumentService
        from plugin.framework.events import EventBus
        self.events = EventBus()
        # DocumentService does not take constructor arguments; it uses the
        # active UNO context when needed.
        self.document = DocumentService()
        self.writer_index = MockWriterIndexService()

@native_test
def test_search_in_document_basic():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass

    tool = SearchInDocument()
    ctx = MockContext(_test_doc, _test_ctx)

    # Simple search
    res = tool.execute(ctx, pattern="needle")
    assert res["status"] == "ok"
    assert res["count"] == 2
    assert len(res["matches"]) == 2

    match1 = res["matches"][0]
    assert match1["text"] == "needle"
    assert match1["paragraph_index"] == 1

    match2 = res["matches"][1]
    # SearchInDocument returns the matched substring with original casing
    # from the paragraph text. The second match comes from "Needles..."
    # so the 6-letter substring is "Needle".
    assert match2["text"] == "Needle"
    assert match2["paragraph_index"] == 2

@native_test
def test_search_in_document_case_sensitive():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass

    tool = SearchInDocument()
    ctx = MockContext(_test_doc, _test_ctx)

    res = tool.execute(ctx, pattern="Needle", case_sensitive=True)
    assert res["status"] == "ok"
    assert res["count"] == 1
    assert res["matches"][0]["paragraph_index"] == 2
    assert res["matches"][0]["text"] == "Needle"

@native_test
def test_search_in_document_regex():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass

    tool = SearchInDocument()
    ctx = MockContext(_test_doc, _test_ctx)

    res = tool.execute(ctx, pattern=r"Needles? are \w+", regex=True)
    assert res["status"] == "ok"
    assert res["count"] == 1
    assert res["matches"][0]["text"] == "Needles are sharp"

@native_test
def test_advanced_search_tool():
    import pytest
    pytest.skip("advanced_search tool currently not exposed to LLM/MCP API")

@native_test
def test_get_index_stats():
    try:
        import pytest
        if _test_doc is None:
            pytest.skip("Requires LibreOffice document from native runner")
    except ImportError:
        pass

    tool = GetIndexStats()
    ctx = MockContext(_test_doc, _test_ctx)

    res = tool.execute(ctx)
    assert res["status"] == "ok"
    assert res["stems"] == 100

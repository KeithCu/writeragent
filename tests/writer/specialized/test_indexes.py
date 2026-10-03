from unittest.mock import MagicMock, patch


from plugin.writer.specialized.indexes import (
    IndexesList,
    IndexesCreate,
    IndexesAddMark,
    IndexesDeleteTocEntry,
    IndexesInsertTocEntry,
    IndexesListCites,
    IndexesListTocEntries,
    _compose_toc_line,
    canonicalize_bibliography_field_name,
    collect_bibliography_field_pairs,
    fields_sequence_to_dict,
    index_kind_from_uno,
    is_bibliography_text_field,
    resolve_bibliographic_type,
)


def test_canonicalize_bibliography_field_name():
    assert canonicalize_bibliography_field_name("Identifier") == "Identifier"
    assert canonicalize_bibliography_field_name("IDENTIFIER") == "Identifier"
    assert canonicalize_bibliography_field_name("identifier") == "Identifier"
    # Live LO spelling (authfld.cxx), not English BibliographicType.
    assert canonicalize_bibliography_field_name("BibiliographicType") == "BibiliographicType"
    assert canonicalize_bibliography_field_name("BIBILIOGRAPHIC_TYPE") == "BibiliographicType"
    assert canonicalize_bibliography_field_name("BibliographicType") == "BibiliographicType"
    assert canonicalize_bibliography_field_name("bibliographic_type") == "BibiliographicType"
    assert canonicalize_bibliography_field_name("zotero_key") is None
    assert canonicalize_bibliography_field_name("citekey") is None


def test_resolve_bibliographic_type():
    assert resolve_bibliographic_type("book") == 1
    assert resolve_bibliographic_type(1) == 1
    assert resolve_bibliographic_type("article") == 0
    assert resolve_bibliographic_type(None) is None
    assert resolve_bibliographic_type("") is None


def test_collect_bibliography_field_pairs_defaults_identifier_from_text():
    pairs = dict(collect_bibliography_field_pairs({
        "text": "Smith2024",
        "author": "Smith, J.",
        "title": "Climate Notes",
        "year": 2024,
        "pages": "12-15",
        "bibliographic_type": "book",
        "zotero_key": "ABCD1234",
    }))
    assert pairs["Identifier"] == "Smith2024"
    assert pairs["Author"] == "Smith, J."
    assert pairs["Title"] == "Climate Notes"
    assert pairs["Year"] == "2024"
    assert pairs["Pages"] == "12-15"
    assert pairs["BibiliographicType"] == 1
    assert "zotero_key" not in pairs


def test_collect_bibliography_field_pairs_identifier_overrides_text():
    pairs = dict(collect_bibliography_field_pairs({
        "text": "ignored",
        "identifier": "Doe2025",
        "fields": {"ISBN": "978-0-00", "Unknown": "x"},
    }))
    assert pairs["Identifier"] == "Doe2025"
    assert pairs["ISBN"] == "978-0-00"
    assert "Unknown" not in pairs


def test_fields_sequence_to_dict_skips_empty():
    class PV:
        def __init__(self, name, value):
            self.Name = name
            self.Value = value

    mapped = fields_sequence_to_dict((
        PV("Identifier", "Smith2024"),
        PV("Author", ""),
        PV("Title", "Climate"),
        PV("BibiliographicType", 1),
    ))
    assert mapped == {"Identifier": "Smith2024", "Title": "Climate", "BibiliographicType": 1}


def test_index_kind_from_uno_prefers_service_name():
    idx = MagicMock()
    idx.getServiceName.return_value = "com.sun.star.text.Bibliography"
    idx.getImplementationName.return_value = "SwXDocumentIndex"
    assert index_kind_from_uno(idx) == "bibliography"


def test_index_kind_from_uno_impl_fallback():
    idx = MagicMock()
    idx.getServiceName.return_value = None
    idx.getImplementationName.return_value = "SwXContentIndex"
    assert index_kind_from_uno(idx) == "toc"

    alpha = MagicMock()
    alpha.getServiceName.return_value = None
    alpha.getImplementationName.return_value = "SwXDocumentIndex"
    assert index_kind_from_uno(alpha) == "alphabetical"


def test_is_bibliography_text_field():
    field = MagicMock()
    field.supportsService.side_effect = lambda name: name.endswith("textfield.Bibliography")
    assert is_bibliography_text_field(field)
    other = MagicMock()
    other.supportsService.return_value = False
    assert not is_bibliography_text_field(other)


def test_indexes_list():
    tool = IndexesList()
    ctx = MagicMock()
    doc = ctx.doc
    indexes_mock = MagicMock()
    doc.getDocumentIndexes.return_value = indexes_mock
    indexes_mock.getCount.return_value = 2

    idx1 = MagicMock()
    idx1.getName.return_value = "Index1"
    idx1.Title = "Title1"
    idx1.getServiceName.return_value = None
    idx1.getImplementationName.return_value = "SwXContentIndex"

    idx2 = MagicMock()
    idx2.getName.return_value = "Index2"
    idx2.Title = "Title2"
    idx2.getServiceName.return_value = None
    idx2.getImplementationName.return_value = "SwXDocumentIndex"

    indexes_mock.getByIndex.side_effect = [idx1, idx2]

    res = tool.execute(ctx)
    assert res["status"] == "ok"
    assert res["count"] == 2
    assert len(res["indexes"]) == 2
    assert res["indexes"][0]["name"] == "Index1"
    assert res["indexes"][0]["title"] == "Title1"
    assert res["indexes"][0]["type"] == "toc"
    assert res["indexes"][1]["name"] == "Index2"
    assert res["indexes"][1]["title"] == "Title2"
    assert res["indexes"][1]["type"] == "alphabetical"


def test_indexes_list_bibliography_via_service_name():
    tool = IndexesList()
    ctx = MagicMock()
    indexes_mock = MagicMock()
    ctx.doc.getDocumentIndexes.return_value = indexes_mock
    indexes_mock.getCount.return_value = 1
    idx = MagicMock()
    idx.getName.return_value = "Bibliography1"
    idx.Title = "References"
    idx.getServiceName.return_value = "com.sun.star.text.Bibliography"
    idx.getImplementationName.return_value = "SwXDocumentIndex"
    indexes_mock.getByIndex.return_value = idx

    res = tool.execute(ctx)
    assert res["indexes"][0]["type"] == "bibliography"


def test_indexes_create():
    tool = IndexesCreate()
    ctx = MagicMock()
    doc = ctx.doc
    cursor_mock = MagicMock()
    doc.getText().createTextCursor.return_value = cursor_mock

    index_mock = MagicMock()
    doc.createInstance.return_value = index_mock

    res = tool.execute(ctx, kind="toc", title="My TOC", create_from_outline=True, target="beginning")
    assert res["status"] == "ok"
    assert res["title"] == "My TOC"

    doc.createInstance.assert_called_with("com.sun.star.text.ContentIndex")
    assert index_mock.Title == "My TOC"
    assert index_mock.CreateFromOutline

    text_mock = cursor_mock.getText()
    text_mock.insertTextContent.assert_called_with(cursor_mock, index_mock, False)
    index_mock.update.assert_called()


def test_indexes_add_mark():
    tool = IndexesAddMark()
    ctx = MagicMock()
    doc = ctx.doc
    cursor_mock = MagicMock()
    doc.getText().createTextCursor.return_value = cursor_mock

    mark_mock = MagicMock()
    doc.createInstance.return_value = mark_mock

    res = tool.execute(ctx, text="Important Term", kind="alphabetical", primary_key="Terms", target="beginning")
    assert res["status"] == "ok"
    assert res["message"] == "Added 'alphabetical' index mark for 'Important Term'"

    doc.createInstance.assert_called_with("com.sun.star.text.DocumentIndexMark")
    assert mark_mock.MarkEntry == "Important Term"
    assert mark_mock.PrimaryKey == "Terms"

    text_mock = cursor_mock.getText()
    text_mock.insertTextContent.assert_called_with(cursor_mock, mark_mock, False)


def test_indexes_add_mark_bibliography_sets_fields_before_insert():
    tool = IndexesAddMark()
    ctx = MagicMock()
    doc = ctx.doc
    cursor_mock = MagicMock()
    doc.getText().createTextCursor.return_value = cursor_mock
    field_mock = MagicMock()
    doc.createInstance.return_value = field_mock

    with patch("plugin.writer.specialized.indexes.set_bibliography_field_values") as set_fields:
        res = tool.execute(
            ctx,
            text="Smith2024",
            kind="bibliography",
            author="Smith, J.",
            title="Climate Notes",
            year="2024",
            target="beginning",
            primary_key="ignored-for-cites",
        )
    assert res["status"] == "ok"
    assert res["identifier"] == "Smith2024"
    assert res["kind"] == "bibliography"
    doc.createInstance.assert_called_with("com.sun.star.text.textfield.Bibliography")
    set_fields.assert_called_once()
    pairs = dict(set_fields.call_args[0][1])
    assert pairs["Identifier"] == "Smith2024"
    assert pairs["Author"] == "Smith, J."
    assert "PrimaryKey" not in pairs
    cursor_mock.getText().insertTextContent.assert_called_with(cursor_mock, field_mock, False)
    # Descriptor Fields must be applied before attach/insert.
    assert set_fields.call_args[0][0] is field_mock


def test_indexes_list_cites_filters_bibliography_fields():
    tool = IndexesListCites()
    ctx = MagicMock()
    fields_enum = MagicMock()
    page_field = MagicMock()
    page_field.supportsService.return_value = False
    cite_field = MagicMock()
    cite_field.supportsService.side_effect = lambda name: "Bibliography" in name

    class PV:
        def __init__(self, name, value):
            self.Name = name
            self.Value = value

    cite_field.getPropertyValue.return_value = (
        PV("Identifier", "Smith2024"),
        PV("Author", "Smith, J."),
        PV("Title", "Climate Notes"),
        PV("Year", "2024"),
        PV("BibiliographicType", 1),
    )
    cite_field.getPresentation.return_value = "[Smith2024]"
    fields_enum.hasMoreElements.side_effect = [True, True, False]
    fields_enum.nextElement.side_effect = [page_field, cite_field]
    ctx.doc.getTextFields.return_value.createEnumeration.return_value = fields_enum

    with patch("plugin.writer.search.describe_match_location", return_value="body"):
        res = tool.execute(ctx)
    assert res["status"] == "ok"
    assert res["count"] == 1
    assert res["fields_scanned"] == 2
    cite = res["cites"][0]
    assert cite["identifier"] == "Smith2024"
    assert cite["author"] == "Smith, J."
    assert cite["title"] == "Climate Notes"
    assert cite["year"] == "2024"
    assert cite["bibliographic_type"] == 1
    assert cite["location"] == "body"
    assert cite["presentation"] == "[Smith2024]"


def test_indexes_create_and_mark_shortened_params():
    tool_create = IndexesCreate()
    ctx = MagicMock()
    doc = ctx.doc
    cursor_mock = MagicMock()
    doc.getText().createTextCursor.return_value = cursor_mock
    index_mock = MagicMock()
    doc.createInstance.return_value = index_mock

    res_create = tool_create.execute(ctx, kind="toc", title="My TOC", target="beginning")
    assert res_create["status"] == "ok"

    tool_mark = IndexesAddMark()
    mark_mock = MagicMock()
    doc.createInstance.return_value = mark_mock
    res_mark = tool_mark.execute(ctx, text="Important Term", kind="alphabetical", target="beginning")
    assert res_mark["status"] == "ok"


def test_compose_toc_line_copies_sibling_page_digits():
    line, page, copied = _compose_toc_line("Gamma title", None, "Alpha title\t1")
    assert line == "Gamma title\t1"
    assert page == "1"
    assert copied is True
    line, page, copied = _compose_toc_line("Gamma title", "4", "Alpha title\t1")
    assert line == "Gamma title\t4"
    assert page == "4"
    assert copied is False
    line, page, copied = _compose_toc_line("Gamma title\t9", None, "Alpha title\t1")
    assert line == "Gamma title\t9"
    assert page == "9"
    assert copied is False


class _TocPara:
    def __init__(self, style, text):
        self.style = style
        self.text = text

    def getString(self):
        return self.text

    def getPropertyValue(self, name):
        if name == "ParaStyleName":
            return self.style
        if name == "ParaTabStops":
            return ()
        raise KeyError(name)


def _one_toc_context():
    ctx = MagicMock()
    doc = ctx.doc
    idx = MagicMock()
    idx.getServiceName.return_value = "com.sun.star.text.ContentIndex"
    indexes = MagicMock()
    indexes.getCount.return_value = 1
    indexes.getByIndex.return_value = idx
    doc.getDocumentIndexes.return_value = indexes
    idx.getPropertyValue.side_effect = lambda name: name == "IsProtected"
    mgr = MagicMock()
    mgr.isLocked.return_value = False
    entered = {}

    def enter(title):
        entered["title"] = title

    mgr.enterUndoContext.side_effect = enter
    mgr.getAllUndoActionTitles.side_effect = lambda: (entered.get("title"),)
    doc.getUndoManager.return_value = mgr
    return ctx, doc, idx, mgr


def test_delete_toc_entry_rejects_markup_and_missing_toc():
    tool = IndexesDeleteTocEntry()
    ctx, _doc, idx, mgr = _one_toc_context()
    markup = tool.execute(ctx, old_content="<b>Alpha</b>")
    assert markup["status"] == "error"
    assert markup["code"] == "INVALID_PARAM"
    idx.update.assert_not_called()
    mgr.enterUndoContext.assert_not_called()

    empty = MagicMock()
    empty.doc.getDocumentIndexes.return_value.getCount.return_value = 0
    missing = tool.execute(empty, old_content="Alpha")
    assert missing["status"] == "error"
    assert "table of contents" in missing["message"]


def test_delete_toc_entry_bad_occurrence_and_dry_run():
    tool = IndexesDeleteTocEntry()
    ctx, _doc, idx, mgr = _one_toc_context()
    para = _TocPara("Contents 1", "Alpha title\t1")
    with patch("plugin.writer.specialized.indexes.toc_match", return_value=(None, "occurrence is past the last table-of-contents match.")):
        bad = tool.execute(ctx, old_content="Alpha", occurrence=3)
    assert bad["status"] == "error"
    assert bad["code"] == "NOT_FOUND"
    assert "occurrence" in bad["message"]
    idx.update.assert_not_called()

    with patch("plugin.writer.specialized.indexes.toc_match", return_value=(para, None)), \
         patch("plugin.writer.specialized.indexes._paragraph_element", return_value=para):
        preview = tool.execute(ctx, old_content="Alpha title", dry_run=True)
    assert preview["status"] == "ok"
    assert preview["dry_run"] is True
    assert preview["text"] == "Alpha title\t1"
    assert preview["text_after"] == ""
    mgr.enterUndoContext.assert_not_called()
    idx.update.assert_not_called()


def test_delete_toc_entry_rolls_back_when_protection_restore_fails():
    tool = IndexesDeleteTocEntry()
    ctx, doc, idx, mgr = _one_toc_context()

    def set_prop(name, value):
        if name == "IsProtected" and value is True:
            raise RuntimeError("restore failed")

    idx.setPropertyValue.side_effect = set_prop
    para = _TocPara("Contents 1", "Alpha title\t1")
    anchor = MagicMock()
    text = MagicMock()
    anchor.getText.return_value = text
    idx.getAnchor.return_value = anchor
    with patch("plugin.writer.specialized.indexes.toc_match", return_value=(para, None)), \
         patch("plugin.writer.specialized.indexes._paragraph_element", return_value=para), \
         patch("plugin.writer.specialized.indexes._remove_paragraph") as remove:
        res = tool.execute(ctx, old_content="Alpha title")
    assert res["status"] == "error"
    assert "protection" in res["message"]
    remove.assert_called_once()
    mgr.undo.assert_called_once()
    idx.update.assert_not_called()
    doc.getUndoManager.assert_called()


def test_delete_toc_entry_undo_unavailable_does_not_unprotect():
    tool = IndexesDeleteTocEntry()
    ctx, _doc, idx, mgr = _one_toc_context()
    mgr.isLocked.return_value = True
    para = _TocPara("Contents 1", "Alpha title\t1")
    with patch("plugin.writer.specialized.indexes.toc_match", return_value=(para, None)), \
         patch("plugin.writer.specialized.indexes._paragraph_element", return_value=para):
        res = tool.execute(ctx, old_content="Alpha title")
    assert res["status"] == "error"
    assert res["code"] == "UNDO_UNAVAILABLE"
    idx.setPropertyValue.assert_not_called()
    idx.update.assert_not_called()


def test_insert_toc_entry_param_errors_and_dry_run():
    tool = IndexesInsertTocEntry()
    ctx, doc, idx, mgr = _one_toc_context()
    styles = MagicMock()
    styles.hasByName.return_value = True
    doc.getStyleFamilies.return_value.getByName.return_value = styles
    sibling = _TocPara("Contents 1", "Alpha title\t1")
    markup = tool.execute(ctx, content="<b>Gamma</b>")
    assert markup["code"] == "INVALID_PARAM"
    both = tool.execute(ctx, content="Gamma\t2", page="3")
    assert both["status"] == "error"
    assert "not both" in both["message"]
    needs_anchor = tool.execute(ctx, content="Gamma", position="before")
    assert needs_anchor["code"] == "INVALID_PARAM"
    bad_url = tool.execute(ctx, content="Gamma", hyperlink_url="#__RefHeading___Toc1")
    assert bad_url["code"] == "INVALID_PARAM"
    assert "outline" in bad_url["message"]
    bad_level = tool.execute(ctx, content="Gamma", level=0)
    assert bad_level["code"] == "INVALID_PARAM"
    idx.update.assert_not_called()

    with patch("plugin.writer.specialized.indexes._paragraphs_in_anchor", return_value=[sibling]):
        preview = tool.execute(ctx, content="Gamma title", dry_run=True)
    assert preview["status"] == "ok"
    assert preview["dry_run"] is True
    assert preview["text_after"] == "Gamma title\t1"
    assert preview["page_from_sibling"] is True
    assert preview["position"] == "end"
    assert preview["para_style"] == "Contents 1"
    mgr.enterUndoContext.assert_not_called()

    with patch("plugin.writer.specialized.indexes._paragraphs_in_anchor", return_value=[sibling]):
        leveled = tool.execute(
            ctx, content="Gamma title", page=4, level=2, dry_run=True,
            hyperlink_url="#1.Gamma title|outline")
    assert leveled["status"] == "ok", leveled
    assert leveled["text_after"] == "Gamma title\t4"
    assert leveled["page_from_sibling"] is False
    assert leveled["para_style"] == "Contents 2"
    assert leveled["hyperlink_url"] == "#1.Gamma title|outline"
    idx.update.assert_not_called()


class _Point:
    def __init__(self, para):
        self.para = para


class _LinkPortion:
    def __init__(self, url):
        self.url = url

    def getPropertyValue(self, name):
        if name == "HyperLinkURL":
            return self.url
        return ""


class _ElementEnum:
    def __init__(self, items):
        self._items = list(items)
        self._at = 0

    def hasMoreElements(self):
        return self._at < len(self._items)

    def nextElement(self):
        item = self._items[self._at]
        self._at += 1
        return item


class _IndexPara:
    """One paragraph the TOC walk can contain or leave outside the anchor."""

    def __init__(self, text, style, urls, inside):
        self.text = text
        self.style = style
        self.urls = list(urls)
        self.inside = inside
        self._start = _Point(self)
        self._end = _Point(self)

    def getString(self):
        return self.text

    def getStart(self):
        return self._start

    def getEnd(self):
        return self._end

    def getPropertyValue(self, name):
        if name == "ParaStyleName":
            return self.style
        raise KeyError(name)

    def createEnumeration(self):
        return _ElementEnum(_LinkPortion(url) for url in self.urls)


class _IndexText:
    def __init__(self, rows):
        self.rows = rows

    def createEnumeration(self):
        return _ElementEnum(self.rows)

    def compareRegionStarts(self, _outer_start, inner_start):
        return 0 if inner_start.para.inside else -1

    def compareRegionEnds(self, inner_end, _outer_end):
        return 0 if inner_end.para.inside else -1


class _IndexAnchor:
    def __init__(self, text):
        self._text = text
        self._start = _Point(None)
        self._end = _Point(None)

    def getText(self):
        return self._text

    def getStart(self):
        return self._start

    def getEnd(self):
        return self._end


def _attach_index_rows(ctx, idx, rows):
    text = _IndexText(rows)
    idx.getAnchor.return_value = _IndexAnchor(text)
    ctx.doc.storeToURL.side_effect = AssertionError("XHTML storeToURL")
    return ctx


def test_list_toc_entries_reads_rows_not_the_full_export():
    """A long linked TOC is listed from index paragraphs, not the XHTML export.

    What was wrong: inspecting rows called get_document_content, which exports
    the whole document through the XHTML Writer filter. A linked TOC is a bad
    input for that filter. This listing must not call that export or update().
    """
    tool = IndexesListTocEntries()
    ctx, _doc, idx, _mgr = _one_toc_context()
    rows = [_IndexPara("Contents", "Contents Heading", [], True)]
    count = 800
    for i in range(count):
        level = 3 if i % 5 == 0 else 1
        rows.append(_IndexPara(
            "Section %d\t%d" % (i, i + 1),
            "Contents %d" % level,
            ["#%d.Section %d|outline" % (level, i)],
            True,
        ))
    rows.append(_IndexPara("Body only heading", "Heading 1", ["#body|outline"], False))
    rows.append(_IndexPara(
        "Bookmark row\t9", "Contents 2", ["#__RefHeading___Toc999"], True))
    rows.append(_IndexPara(
        "Linked both\t2",
        "Contents 1",
        ["https://example.invalid/toc", "#1.Linked both|outline"],
        True,
    ))
    _attach_index_rows(ctx, idx, rows)
    idx.update.side_effect = AssertionError("ContentIndex.update")

    with patch("plugin.writer.html_export._export_xhtml", side_effect=AssertionError("xhtml export")) as export_xhtml, \
         patch("plugin.writer.html_export.document_to_content", side_effect=AssertionError("full export")) as export_doc, \
         patch("plugin.writer.format.document_to_content", side_effect=AssertionError("format export")) as export_format, \
         patch("plugin.writer.content.GetDocumentContent.execute", side_effect=AssertionError("get_document_content")) as export_tool:
        listed = tool.execute(ctx)

    assert listed["status"] == "ok", listed
    assert listed["index"] == 0
    assert listed["count"] == count + 2
    assert export_xhtml.call_count == 0
    assert export_doc.call_count == 0
    assert export_format.call_count == 0
    assert export_tool.call_count == 0
    ctx.doc.storeToURL.assert_not_called()
    first = listed["entries"][0]
    assert first == {
        "text": "Section 0\t1",
        "level": 3,
        "hyperlink_url": "#3.Section 0|outline",
    }
    plain = listed["entries"][1]
    assert plain["text"] == "Section 1\t2"
    assert plain["level"] == 1
    assert plain["hyperlink_url"] == "#1.Section 1|outline"
    texts = [row["text"] for row in listed["entries"]]
    assert "Contents" not in texts
    assert "Body only heading" not in texts
    bookmark = listed["entries"][-2]
    assert bookmark["text"] == "Bookmark row\t9"
    assert bookmark["level"] == 2
    assert bookmark["hyperlink_url"] == "#__RefHeading___Toc999"
    both = listed["entries"][-1]
    assert both["text"] == "Linked both\t2"
    assert both["hyperlink_url"] == "#1.Linked both|outline"
    assert "https://example.invalid/toc" not in both["hyperlink_url"]


def test_list_toc_entries_rejects_a_missing_or_non_toc_index():
    tool = IndexesListTocEntries()
    empty = MagicMock()
    empty.doc.getDocumentIndexes.return_value.getCount.return_value = 0
    missing = tool.execute(empty)
    assert missing["status"] == "error"
    assert missing["code"] == "INVALID_PARAM"
    assert "table of contents" in missing["message"]

    ctx = MagicMock()
    indexes = MagicMock()
    indexes.getCount.return_value = 2
    toc_a = MagicMock()
    toc_a.getServiceName.return_value = "com.sun.star.text.ContentIndex"
    toc_b = MagicMock()
    toc_b.getServiceName.return_value = "com.sun.star.text.ContentIndex"
    indexes.getByIndex.side_effect = lambda i: (toc_a, toc_b)[i]
    ctx.doc.getDocumentIndexes.return_value = indexes

    needs_index = tool.execute(ctx)
    assert needs_index["status"] == "error"
    assert "exactly one table of contents" in needs_index["message"]
    toc_a.update.assert_not_called()
    toc_b.update.assert_not_called()

    alphabetical = MagicMock()
    alphabetical.getServiceName.return_value = "com.sun.star.text.DocumentIndex"
    toc = MagicMock()
    toc.getServiceName.return_value = "com.sun.star.text.ContentIndex"
    toc.getAnchor.side_effect = RuntimeError("disposed anchor")
    indexes.getByIndex.side_effect = lambda i: (alphabetical, toc)[i]

    not_toc = tool.execute(ctx, index=0)
    assert not_toc["status"] == "error"
    assert "not a table of contents" in not_toc["message"]
    alphabetical.update.assert_not_called()

    unreadable = tool.execute(ctx, index=1)
    assert unreadable["status"] == "error"
    assert unreadable["code"] == "TOOL_EXECUTION_ERROR"
    toc.update.assert_not_called()

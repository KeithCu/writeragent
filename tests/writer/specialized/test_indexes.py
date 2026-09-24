from unittest.mock import MagicMock, patch


from plugin.writer.specialized.indexes import (
    IndexesList,
    IndexesCreate,
    IndexesAddMark,
    IndexesListCites,
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

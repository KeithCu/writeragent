
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("writer")
def test_get_page_for_paragraph_with_table(ctx, doc):
    """Test get_page_for_paragraph resolves correctly when tables exist."""
    import time
    import uno
    from plugin.doc.document_helpers import DocumentService
    text = doc.getText()
    cursor = text.createTextCursor()

    # 0: Page 1, paragraph
    text.insertString(cursor, "First paragraph\n", False)

    # 1: Page 1, table (2x2)
    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(2, 2)
    text.insertTextContent(cursor, table, False)

    # 2: Page 1, paragraph after table
    text.insertString(cursor, "Paragraph after table", False)
    text.insertControlCharacter(cursor, 0, False)

    # 3: Page 2, paragraph
    cursor.setPropertyValue("BreakType", uno.Enum("com.sun.star.style.BreakType", "PAGE_BEFORE"))
    text.insertString(cursor, "Page 2 paragraph", False)

    # Let layout settle
    # Give it a moment to layout pages, as getPage relies on view layout
    time.sleep(0.5)

    ds = DocumentService()

    # Test paragraph before table
    page = ds.get_page_for_paragraph(doc, 0)
    assert page == 1

    # Test table itself (index 1)
    page = ds.get_page_for_paragraph(doc, 1)
    assert page == 1

    # Test paragraph after table (index 2)
    page = ds.get_page_for_paragraph(doc, 2)
    assert page == 1

    # Test paragraph on page 2 (index 3)
    page = ds.get_page_for_paragraph(doc, 3)
    assert page == 2

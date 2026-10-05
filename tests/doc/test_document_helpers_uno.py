
def test_get_page_for_paragraph_with_table(writer_doc):
    """Test get_page_for_paragraph resolves correctly when tables exist."""
    import time
    import uno
    from plugin.doc.document_helpers import DocumentService

    doc = writer_doc
    text = doc.getText()
    cursor = text.createTextCursor()

    # 0: Page 1, paragraph
    text.insertString(cursor, "First paragraph\n", False)

    # 1: Page 1, table (2x2)
    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(2, 2)
    text.insertTextContent(cursor, table, False)

    # 2: Page 1, paragraph after table
    text.insertString(cursor, "Paragraph after table\n", False)

    # Page break to Page 2
    cursor.setPropertyValue("BreakType", uno.Enum("com.sun.star.style.BreakType", "PAGE_BEFORE"))

    # 3: Page 2, paragraph
    text.insertString(cursor, "Page 2 paragraph\n", False)

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

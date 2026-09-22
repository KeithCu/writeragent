"""Native tests for indexes bibliography v1 (cite insert, list, table)."""

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc
from plugin.writer.specialized.indexes import (
    IndexesAddMark,
    IndexesCreate,
    IndexesList,
    IndexesListCites,
    IndexesRefreshTocEntry,
    IndexesUpdateAll,
)


def _tool_ctx(ctx, doc):
    return TestingFactory.create_context(doc=doc, ctx=ctx, env="native", doc_type="writer")


@native_test
@with_native_doc("writer")
def test_indexes_cite_insert_list_and_table(ctx, doc):
    """Insert a cite, list it, place the bibliography table, then update."""
    doc.getText().setString("See the climate review. ")
    tctx = _tool_ctx(ctx, doc)

    inserted = IndexesAddMark().execute(
        tctx,
        text="Smith2024",
        kind="bibliography",
        author="Smith, J.",
        title="Climate Notes",
        year="2024",
        pages="12-15",
        bibliographic_type="book",
        target="end",
    )
    assert inserted.get("status") == "ok", inserted
    assert inserted.get("identifier") == "Smith2024"
    assert inserted["fields"]["BibiliographicType"] == 1

    listed = IndexesListCites().execute(tctx)
    assert listed.get("status") == "ok", listed
    assert listed.get("count") == 1
    cite = listed["cites"][0]
    assert cite["identifier"] == "Smith2024"
    assert cite["author"] == "Smith, J."
    assert cite["title"] == "Climate Notes"
    assert cite["year"] == "2024"
    assert cite["pages"] == "12-15"
    assert cite["bibliographic_type"] == 1
    assert cite["presentation"] == "[Smith2024]"
    assert cite["location"]

    created = IndexesCreate().execute(
        tctx, kind="bibliography", title="References", target="end"
    )
    assert created.get("status") == "ok", created

    indexes = IndexesList().execute(tctx)
    assert indexes.get("status") == "ok", indexes
    kinds = [item["type"] for item in indexes["indexes"]]
    assert "bibliography" in kinds, indexes

    refreshed = IndexesUpdateAll().execute(tctx)
    assert refreshed.get("status") == "ok", refreshed

    body = doc.getText().getString()
    assert "Smith2024" in body
    assert "Climate Notes" in body
    assert "2024" in body


@native_test
@with_native_doc("writer")
def test_indexes_list_bibliography_kind_uses_service_name(ctx, doc):
    """getImplementationName() is SwXDocumentIndex; kind must still be bibliography."""
    tctx = _tool_ctx(ctx, doc)
    IndexesCreate().execute(tctx, kind="bibliography", title="Works Cited", target="end")
    listed = IndexesList().execute(tctx)
    assert listed.get("count") >= 1, listed
    bib = next(item for item in listed["indexes"] if item["type"] == "bibliography")
    assert bib["title"] == "Works Cited"


def _add_heading(text, title, first):
    cursor = text.createTextCursor()
    cursor.gotoEnd(False)
    if not first:
        text.insertControlCharacter(cursor, 0, False)
        cursor.gotoEnd(False)
    text.insertString(cursor, title, False)
    sel = text.createTextCursorByRange(cursor.getEnd())
    if not sel.goLeft(len(title), True):
        raise AssertionError("could not select heading %r" % title)
    sel.setPropertyValue("ParaStyleName", "Heading 1")


def _tab_paragraphs(doc):
    """Paragraphs that contain a tab. Generated TOC entries do; body headings do not."""
    enum = doc.getText().createEnumeration()
    found = []
    while enum.hasMoreElements():
        para = enum.nextElement()
        try:
            chunk = para.getString() or ""
        except Exception:
            continue
        if "\t" in chunk:
            found.append(para)
    return found


def _paint_entry(text, para, url, color=None):
    cur = text.createTextCursorByRange(para.getStart())
    cur.gotoRange(para.getEnd(), True)
    if color is not None:
        cur.setPropertyValue("CharColor", color)
    if url:
        cur.setPropertyValue("HyperLinkURL", url)


def _portion_color(doc, needle):
    enum = doc.getText().createEnumeration()
    while enum.hasMoreElements():
        para = enum.nextElement()
        try:
            if needle not in (para.getString() or ""):
                continue
            portions = para.createEnumeration()
        except Exception:
            continue
        while portions.hasMoreElements():
            portion = portions.nextElement()
            try:
                chunk = portion.getString() or ""
            except Exception:
                chunk = ""
            if needle in chunk or (chunk and chunk in needle):
                try:
                    return portion.getPropertyValue("CharColor")
                except Exception:
                    return None
    return None


@native_test
@with_native_doc("writer")
def test_toc_update_clears_direct_character_color_uno(ctx, doc):
    """indexes_update_all rebuilds the TOC. A direct color on one entry does not survive.

    A default content index is protected. The one-entry tool has to clear that
    around its edit; this test only records the flag and the update() wipe.
    """
    text = doc.getText()
    text.setString("")
    _add_heading(text, "Alpha title", True)
    tctx = _tool_ctx(ctx, doc)
    created = IndexesCreate().execute(tctx, kind="toc", title="Contents", target="beginning")
    assert created.get("status") == "ok", created
    indexes = doc.getDocumentIndexes()
    toc = indexes.getByIndex(0)
    protected = bool(toc.getPropertyValue("IsProtected"))
    assert protected is True, protected
    entries = _tab_paragraphs(doc)
    assert entries, doc.getText().getString()
    toc.IsProtected = False
    _paint_entry(text, entries[0], "", color=0x0000FF)
    assert _portion_color(doc, "Alpha title") == 0x0000FF
    toc.update()
    assert _portion_color(doc, "Alpha title") != 0x0000FF


@native_test
@with_native_doc("writer")
def test_refresh_toc_entry_keeps_color_page_and_body_uno(ctx, doc):
    """One TOC title changes. The color, the other entry, the page number, and the body heading stay."""
    text = doc.getText()
    text.setString("")
    _add_heading(text, "Alpha title", True)
    _add_heading(text, "Beta title", False)
    tctx = _tool_ctx(ctx, doc)
    created = IndexesCreate().execute(tctx, kind="toc", title="Contents", target="beginning")
    assert created.get("status") == "ok", created
    toc = doc.getDocumentIndexes().getByIndex(0)
    entries = _tab_paragraphs(doc)
    assert len(entries) >= 2, [para.getString() for para in entries]
    protected = bool(toc.getPropertyValue("IsProtected"))
    if protected:
        toc.IsProtected = False
    alpha = next(para for para in entries if "Alpha title" in (para.getString() or ""))
    beta = next(para for para in entries if "Beta title" in (para.getString() or ""))
    outline = "#1.Alpha title|outline"
    bookmark = "#__RefHeading___Toc999"
    _paint_entry(text, alpha, outline, color=0x0000FF)
    _paint_entry(text, beta, bookmark)
    if protected:
        toc.IsProtected = True
    beta_before = beta.getString()
    page = alpha.getString().split("\t", 1)[1]

    preview = IndexesRefreshTocEntry().execute(
        tctx, old_content="Alpha title", content="Alpha renamed", dry_run=True)
    assert preview.get("status") == "ok", preview
    assert preview.get("dry_run") is True, preview
    assert "Alpha title" in preview.get("text", ""), preview
    assert "Alpha renamed" in preview.get("text_after", ""), preview
    assert page.strip() in preview.get("text_after", ""), preview
    assert preview.get("hyperlink_url") == outline, preview
    assert preview.get("hyperlink_url_after") == "#1.Alpha renamed|outline", preview
    assert "Alpha renamed" not in doc.getText().getString()

    edited = IndexesRefreshTocEntry().execute(
        tctx, old_content="Alpha title", content="Alpha renamed")
    assert edited.get("status") == "ok", edited
    assert edited.get("hyperlink_url_after") == "#1.Alpha renamed|outline", edited
    assert "Alpha title" in edited.get("text", ""), edited
    assert "Alpha renamed" in edited.get("text_after", ""), edited
    assert page.strip() in edited.get("text_after", ""), edited
    body = doc.getText().getString()
    assert "Alpha renamed" in body, body
    assert "Alpha title" in body, body
    assert page in body, body
    assert beta.getString() == beta_before, (beta.getString(), beta_before)
    assert _portion_color(doc, "Alpha renamed") == 0x0000FF
    enum = doc.getText().createEnumeration()
    beta_url = ""
    alpha_url = ""
    while enum.hasMoreElements():
        para = enum.nextElement()
        try:
            chunk = para.getString() or ""
            portions = para.createEnumeration()
        except Exception:
            continue
        while portions.hasMoreElements():
            portion = portions.nextElement()
            try:
                url = portion.getPropertyValue("HyperLinkURL") or ""
            except Exception:
                url = ""
            piece = ""
            try:
                piece = portion.getString() or ""
            except Exception:
                pass
            if "Beta title" in chunk and url:
                beta_url = url
            if "Alpha renamed" in piece and url:
                alpha_url = url
    assert alpha_url == "#1.Alpha renamed|outline", alpha_url
    assert beta_url == bookmark, beta_url
    assert bool(toc.getPropertyValue("IsProtected")) is protected

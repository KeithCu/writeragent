"""Native tests for indexes bibliography v1 (cite insert, list, table)."""

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc
from plugin.writer.specialized.indexes import (
    IndexesAddMark,
    IndexesCreate,
    IndexesDeleteTocEntry,
    IndexesInsertTocEntry,
    IndexesList,
    IndexesListCites,
    IndexesListTocEntries,
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


def _paragraph_looks(para):
    """(chunk, HyperLinkURL, CharColor, CharUnderline) for visible portions of *para*."""
    rows = []
    try:
        portions = para.createEnumeration()
    except Exception:
        return rows
    while portions.hasMoreElements():
        portion = portions.nextElement()
        try:
            chunk = portion.getString() or ""
        except Exception:
            chunk = ""
        if not chunk:
            continue
        try:
            url = portion.getPropertyValue("HyperLinkURL") or ""
        except Exception:
            url = ""
        try:
            color = portion.getPropertyValue("CharColor")
        except Exception:
            color = None
        try:
            underline = portion.getPropertyValue("CharUnderline")
        except Exception:
            underline = None
        rows.append((chunk, url, color, underline))
    return rows


@native_test
@with_native_doc("writer")
def test_refresh_toc_entry_keeps_black_and_no_underline_uno(ctx, doc):
    """indexes_refresh_toc_entry must not apply Internet-link chrome to a black TOC line.

    The title is replaced with a longer string so new fragments are created.
    Numbering and the page number were black / not underlined before and stay that way.
    """
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
    cur = text.createTextCursorByRange(alpha.getStart())
    cur.gotoRange(alpha.getEnd(), True)
    # URL first (Internet-link defaults), then the customized black / no-underline.
    cur.setPropertyValue("HyperLinkURL", outline)
    cur.setPropertyValue("CharColor", 0)
    cur.setPropertyValue("CharUnderline", 0)
    if protected:
        toc.IsProtected = True
    beta_before = beta.getString()
    page = alpha.getString().split("\t", 1)[1]
    before_looks = _paragraph_looks(alpha)
    assert before_looks, before_looks
    assert all(color == 0 and underline == 0 for _chunk, _url, color, underline in before_looks), (
        before_looks)

    renamed = "Alpha renamed much longer"
    edited = IndexesRefreshTocEntry().execute(
        tctx, old_content="Alpha title", content=renamed)
    assert edited.get("status") == "ok", edited
    assert edited.get("hyperlink_url_after") == "#1." + renamed + "|outline", edited
    assert page.strip() in edited.get("text_after", ""), edited
    body = doc.getText().getString()
    assert renamed in body, body
    assert "Alpha title" in body, body
    assert page in body, body
    assert beta.getString() == beta_before, (beta.getString(), beta_before)

    entries = _tab_paragraphs(doc)
    alpha_after = next(para for para in entries if renamed in (para.getString() or ""))
    looks = _paragraph_looks(alpha_after)
    assert looks, looks
    joined = "".join(chunk for chunk, _url, _color, _underline in looks)
    assert renamed in joined, looks
    assert page.strip() in joined, looks
    title_rows = [
        row for row in looks
        if renamed in row[0] or (row[0] and row[0] in renamed)
    ]
    assert title_rows, looks
    for chunk, url, color, underline in looks:
        assert color == 0, (chunk, color, looks)
        assert underline == 0, (chunk, underline, looks)
    assert any(url == "#1." + renamed + "|outline" for _chunk, url, _color, _underline in title_rows), (
        looks)
    assert bool(toc.getPropertyValue("IsProtected")) is protected


def _entry_strings(doc):
    return [para.getString() or "" for para in _tab_paragraphs(doc)]


def _tab_positions(para):
    try:
        tabs = list(para.getPropertyValue("ParaTabStops") or ())
    except Exception:
        return []
    return [int(getattr(stop, "Position", -1)) for stop in tabs]


def _set_direct_tabs(para, position):
    """Move the row's existing tab to *position*. Typed []TabStop — a bare tuple is rejected."""
    import uno

    text = para.getText()
    cursor = text.createTextCursorByRange(para.getStart())
    cursor.gotoEndOfParagraph(True)
    existing = list(para.getPropertyValue("ParaTabStops") or ())
    stop = uno.createUnoStruct("com.sun.star.style.TabStop")
    if existing:
        src = existing[0]
        stop.Alignment = src.Alignment
        stop.FillChar = src.FillChar
        stop.DecimalChar = src.DecimalChar
    stop.Position = position
    uno.invoke(
        cursor,
        "setPropertyValue",
        ("ParaTabStops", uno.Any("[]com.sun.star.style.TabStop", (stop,))),
    )


@native_test
@with_native_doc("writer")
def test_delete_toc_entry_removes_one_row_uno(ctx, doc):
    """One obsolete TOC row goes away. The neighbor, its color, and the body heading stay.

    indexes_update_all would rebuild the row from the heading and wipe the direct
    color. The row staying gone, with the neighbor still green, means update()
    did not run.
    """
    text = doc.getText()
    text.setString("")
    _add_heading(text, "Alpha title", True)
    _add_heading(text, "Beta title", False)
    tctx = _tool_ctx(ctx, doc)
    created = IndexesCreate().execute(tctx, kind="toc", title="Contents", target="beginning")
    assert created.get("status") == "ok", created
    toc = doc.getDocumentIndexes().getByIndex(0)
    entries = _tab_paragraphs(doc)
    assert len(entries) >= 2, _entry_strings(doc)
    protected = bool(toc.getPropertyValue("IsProtected"))
    if protected:
        toc.IsProtected = False
    alpha = next(para for para in entries if "Alpha title" in (para.getString() or ""))
    beta = next(para for para in entries if "Beta title" in (para.getString() or ""))
    _paint_entry(text, alpha, "#1.Alpha title|outline", color=0x0000FF)
    _paint_entry(text, beta, "#__RefHeading___Toc999", color=0x00FF00)
    if protected:
        toc.IsProtected = True
    beta_before = beta.getString()
    mgr = doc.getUndoManager()
    titles_before = list(mgr.getAllUndoActionTitles())

    preview = IndexesDeleteTocEntry().execute(tctx, old_content="Alpha title", dry_run=True)
    assert preview.get("status") == "ok", preview
    assert preview.get("dry_run") is True, preview
    assert "Alpha title" in preview.get("text", ""), preview
    assert preview.get("text_after") == "", preview
    assert list(mgr.getAllUndoActionTitles()) == titles_before
    assert "Alpha title\t" in doc.getText().getString()

    deleted = IndexesDeleteTocEntry().execute(tctx, old_content="Alpha title")
    assert deleted.get("status") == "ok", deleted
    assert deleted.get("dry_run") is False, deleted
    assert "Alpha title" in deleted.get("text", ""), deleted
    rows = _entry_strings(doc)
    assert not any(row.startswith("Alpha title") for row in rows), rows
    assert any(row.startswith("Beta title") for row in rows), rows
    assert beta.getString() == beta_before, (beta.getString(), beta_before)
    body = doc.getText().getString()
    assert "Alpha title" in body, body
    assert "Beta title" in body, body
    beta_para = next(para for para in _tab_paragraphs(doc) if "Beta title" in (para.getString() or ""))
    looks = _paragraph_looks(beta_para)
    assert looks, looks
    assert all(color == 0x00FF00 for _chunk, _url, color, _underline in looks), looks
    assert any(url == "#__RefHeading___Toc999" for _chunk, url, _color, _underline in looks), looks
    assert bool(toc.getPropertyValue("IsProtected")) is protected

    titles = list(mgr.getAllUndoActionTitles())
    assert titles and str(titles[0]).startswith("WriterAgent edit"), titles
    mgr.undo()
    restored = _entry_strings(doc)
    assert any(row.startswith("Alpha title") for row in restored), restored
    assert any(row.startswith("Beta title") for row in restored), restored
    assert bool(toc.getPropertyValue("IsProtected")) is protected


@native_test
@with_native_doc("writer")
def test_delete_last_toc_entry_and_bad_occurrence_uno(ctx, doc):
    """The last TOC row can be removed, and a past-the-end occurrence does not edit."""
    text = doc.getText()
    text.setString("")
    _add_heading(text, "Alpha title", True)
    _add_heading(text, "Beta title", False)
    tctx = _tool_ctx(ctx, doc)
    created = IndexesCreate().execute(tctx, kind="toc", title="Contents", target="end")
    assert created.get("status") == "ok", created
    toc = doc.getDocumentIndexes().getByIndex(0)
    protected = bool(toc.getPropertyValue("IsProtected"))
    rows = _entry_strings(doc)
    assert len(rows) >= 2, rows
    missing = IndexesDeleteTocEntry().execute(tctx, old_content="Alpha title", occurrence=5)
    assert missing.get("status") == "error", missing
    assert "occurrence" in missing.get("message", ""), missing
    assert _entry_strings(doc) == rows

    deleted = IndexesDeleteTocEntry().execute(tctx, old_content="Beta title")
    assert deleted.get("status") == "ok", deleted
    after = _entry_strings(doc)
    assert any(row.startswith("Alpha title") for row in after), after
    assert not any(row.startswith("Beta title") for row in after), after
    body = doc.getText().getString()
    assert "Beta title" in body, body
    assert bool(toc.getPropertyValue("IsProtected")) is protected


@native_test
@with_native_doc("writer")
def test_insert_toc_entry_adds_one_row_uno(ctx, doc):
    """One missing TOC row is inserted. Neighbors, their color, and their text stay.

    The new row clones Contents N, the sibling's character look, and tab stops.
    A direct color that survives means update() did not rebuild the index.
    Gamma is not a heading, so a rebuild would also drop the new row.
    """
    text = doc.getText()
    text.setString("")
    _add_heading(text, "Alpha title", True)
    _add_heading(text, "Beta title", False)
    tctx = _tool_ctx(ctx, doc)
    created = IndexesCreate().execute(tctx, kind="toc", title="Contents", target="beginning")
    assert created.get("status") == "ok", created
    toc = doc.getDocumentIndexes().getByIndex(0)
    entries = _tab_paragraphs(doc)
    assert len(entries) >= 2, _entry_strings(doc)
    protected = bool(toc.getPropertyValue("IsProtected"))
    if protected:
        toc.IsProtected = False
    alpha = next(para for para in entries if "Alpha title" in (para.getString() or ""))
    beta = next(para for para in entries if "Beta title" in (para.getString() or ""))
    outline = "#1.Alpha title|outline"
    _paint_entry(text, alpha, outline, color=0x0000FF)
    cur = text.createTextCursorByRange(alpha.getStart())
    cur.gotoRange(alpha.getEnd(), True)
    cur.setPropertyValue("CharUnderline", 0)
    _paint_entry(text, beta, "#__RefHeading___Toc999", color=0x00FF00)
    _set_direct_tabs(alpha, 14000)
    if protected:
        toc.IsProtected = True
    alpha_before = alpha.getString()
    beta_before = beta.getString()
    alpha_tabs = _tab_positions(alpha)
    assert 14000 in alpha_tabs, alpha_tabs
    mgr = doc.getUndoManager()
    titles_before = list(mgr.getAllUndoActionTitles())

    preview = IndexesInsertTocEntry().execute(
        tctx, content="Gamma title", position="after", old_content="Alpha title", dry_run=True)
    assert preview.get("status") == "ok", preview
    assert preview.get("dry_run") is True, preview
    assert preview.get("text_after", "").startswith("Gamma title\t"), preview
    assert preview.get("page_from_sibling") is True, preview
    assert preview.get("para_style") == "Contents 1", preview
    assert "Gamma title" not in doc.getText().getString()
    assert list(mgr.getAllUndoActionTitles()) == titles_before

    gamma_url = "#1.Gamma title|outline"
    inserted = IndexesInsertTocEntry().execute(
        tctx,
        content="Gamma title",
        page="4",
        position="after",
        old_content="Alpha title",
        hyperlink_url=gamma_url,
    )
    assert inserted.get("status") == "ok", inserted
    assert inserted.get("dry_run") is False, inserted
    assert inserted.get("text_after", "").startswith("Gamma title\t4"), inserted
    assert inserted.get("page_from_sibling") is False, inserted
    assert inserted.get("para_style") == "Contents 1", inserted
    rows = _entry_strings(doc)
    assert alpha_before in rows, (rows, alpha_before)
    assert beta_before in rows, (rows, beta_before)
    assert rows[0].startswith("Alpha title"), rows
    assert rows[1].startswith("Gamma title"), rows
    assert rows[2].startswith("Beta title"), rows
    gamma = next(para for para in _tab_paragraphs(doc) if "Gamma title" in (para.getString() or ""))
    assert gamma.getPropertyValue("ParaStyleName") == "Contents 1"
    assert 14000 in _tab_positions(gamma), _tab_positions(gamma)
    looks = _paragraph_looks(gamma)
    assert looks, looks
    assert any(url == gamma_url for _chunk, url, _color, _underline in looks), looks
    for chunk, _url, color, underline in looks:
        assert color == 0x0000FF, (chunk, color, looks)
        assert underline == 0, (chunk, underline, looks)
    beta_para = next(para for para in _tab_paragraphs(doc) if "Beta title" in (para.getString() or ""))
    beta_looks = _paragraph_looks(beta_para)
    assert all(color == 0x00FF00 for _chunk, _url, color, _underline in beta_looks), beta_looks
    assert bool(toc.getPropertyValue("IsProtected")) is protected

    mgr.undo()
    undone = _entry_strings(doc)
    assert not any(row.startswith("Gamma title") for row in undone), undone
    assert any(row.startswith("Alpha title") for row in undone), undone
    assert any(row.startswith("Beta title") for row in undone), undone

    before = IndexesInsertTocEntry().execute(
        tctx,
        content="Gamma title",
        page="2",
        position="before",
        old_content="Beta title",
        level=2,
        hyperlink_url=gamma_url,
    )
    assert before.get("status") == "ok", before
    assert before.get("para_style") == "Contents 2", before
    rows = _entry_strings(doc)
    assert [row.split("\t", 1)[0] for row in rows[:3]] == ["Alpha title", "Gamma title", "Beta title"], rows
    assert alpha_before in rows, (rows, alpha_before)
    assert beta_before in rows, (rows, beta_before)
    gamma = next(para for para in _tab_paragraphs(doc) if "Gamma title" in (para.getString() or ""))
    assert gamma.getPropertyValue("ParaStyleName") == "Contents 2"
    assert bool(toc.getPropertyValue("IsProtected")) is protected


@native_test
@with_native_doc("writer")
def test_toc_entry_insert_delete_without_a_toc_uno(ctx, doc):
    """No content index: insert and delete report that, and do not create one."""
    text = doc.getText()
    text.setString("Just a paragraph.")
    tctx = _tool_ctx(ctx, doc)
    deleted = IndexesDeleteTocEntry().execute(tctx, old_content="paragraph")
    assert deleted.get("status") == "error", deleted
    assert "table of contents" in deleted.get("message", ""), deleted
    inserted = IndexesInsertTocEntry().execute(tctx, content="Ghost")
    assert inserted.get("status") == "error", inserted
    assert "table of contents" in inserted.get("message", ""), inserted
    assert doc.getDocumentIndexes().getCount() == 0
    assert doc.getText().getString() == "Just a paragraph."


@native_test
@with_native_doc("writer")
def test_list_toc_entries_reads_linked_rows_without_export_uno(ctx, doc):
    """Visible text, level, and hyperlink come from the index, not XHTML export.

    A linked TOC is a bad input for the XHTML Writer filter. Listing must not
    call that export or ContentIndex.update().
    """
    from unittest.mock import patch

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
    _paint_entry(text, alpha, outline)
    _paint_entry(text, beta, bookmark)
    if protected:
        toc.IsProtected = True
    before = doc.getText().getString()

    def _refuse_export(*_args, **_kwargs):
        raise AssertionError("full document export")

    with patch("plugin.writer.html_export._export_xhtml", side_effect=_refuse_export), \
         patch("plugin.writer.html_export.document_to_content", side_effect=_refuse_export), \
         patch("plugin.writer.format.document_to_content", side_effect=_refuse_export):
        listed = IndexesListTocEntries().execute(tctx)

    assert listed.get("status") == "ok", listed
    assert doc.getText().getString() == before
    assert bool(toc.getPropertyValue("IsProtected")) is protected
    rows = listed["entries"]
    alpha_row = next(row for row in rows if "Alpha title" in row["text"])
    beta_row = next(row for row in rows if "Beta title" in row["text"])
    assert alpha_row["level"] == 1, alpha_row
    assert beta_row["level"] == 1, beta_row
    assert "\t" in alpha_row["text"], alpha_row
    assert alpha_row["hyperlink_url"] == outline, alpha_row
    assert beta_row["hyperlink_url"] == bookmark, beta_row
    assert all(row["text"].strip() != "Contents" for row in rows), rows

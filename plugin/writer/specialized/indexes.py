# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Writer document indexes (TOC, bibliography) — specialized indexes domain.

Bibliography v1 is an indexes overload, not ``domain=bibliography``. Cites are
``com.sun.star.text.textfield.Bibliography`` (a TextField), not index marks.
The reference table is ``indexes_create(kind="bibliography")``. After cite
changes, ``indexes_update_all`` refreshes that table. One TOC row is
``indexes_refresh_toc_entry`` / ``indexes_insert_toc_entry`` /
``indexes_delete_toc_entry``. Those three do not call ``update()`` and do not
edit neighboring rows. ``indexes_update_all`` still rebuilds a TOC and drops
customized formatting. Outline ``HyperLinkURL`` on refresh goes through
``hyperlink_fixup``, which skips a title-only URL rewrite when the whole-span
write already set it. Insert sets ``#…|outline`` on the new row only.
"""

from typing import Any, Callable, cast

from ..specialized_base import ToolWriterIndexBase
from ..target_resolver import resolve_target_cursor

# Creation service → indexes_create kind. Prefer XDocumentIndex.getServiceName()
# when listing: bibliography tables implement SwXDocumentIndex (same as
# alphabetical), so getImplementationName() alone remaps them wrongly.
_INDEX_SERVICE_TO_KIND = {
    "com.sun.star.text.ContentIndex": "toc",
    "com.sun.star.text.DocumentIndex": "alphabetical",
    "com.sun.star.text.UserIndex": "user",
    "com.sun.star.text.IllustrationsIndex": "illustration",
    "com.sun.star.text.TableIndex": "table",
    "com.sun.star.text.ObjectIndex": "object",
    "com.sun.star.text.Bibliography": "bibliography",
}

# Fallback only. SwXDocumentIndex is shared by alphabetical *and* bibliography.
_INDEX_IMPL_TO_KIND = {
    "SwXContentIndex": "toc",
    "SwXDocumentIndex": "alphabetical",
    "SwXUserIndex": "user",
}

# Live LO (sw/source/core/fields/authfld.cxx aFieldNames). PropertyValue.Name is
# the pretty string, not IDENTIFIER. Type is BibiliographicType (IDL typo
# BIBILIOGRAPHIC_TYPE — one L missing, "Bibi…" not "Biblio…").
_BIB_FIELD_NAMES = (
    "Identifier",
    "BibiliographicType",
    "Address",
    "Annote",
    "Author",
    "Booktitle",
    "Chapter",
    "Edition",
    "Editor",
    "Howpublished",
    "Institution",
    "Journal",
    "Month",
    "Note",
    "Number",
    "Organizations",
    "Pages",
    "Publisher",
    "School",
    "Series",
    "Title",
    "Report_Type",
    "Volume",
    "Year",
    "URL",
    "Custom1",
    "Custom2",
    "Custom3",
    "Custom4",
    "Custom5",
    "ISBN",
    "LocalURL",
    "TargetType",
    "TargetURL",
)

_BIB_FIELD_NAME_SET = frozenset(_BIB_FIELD_NAMES)

# Convenience / IDL / English aliases → live Fields names. Unused kinds ignore
# these kwargs; v2 keys (zotero_key, citekey, locator, csl_style) stay unmapped.
_BIB_FIELD_ALIASES = {
    "identifier": "Identifier",
    "author": "Author",
    "title": "Title",
    "year": "Year",
    "pages": "Pages",
    "isbn": "ISBN",
    "url": "URL",
    "booktitle": "Booktitle",
    "publisher": "Publisher",
    "bibiliographictype": "BibiliographicType",
    "bibliographictype": "BibiliographicType",
    "bibliographic_type": "BibiliographicType",
    "bibiliographic_type": "BibiliographicType",
    "bibilographic_type": "BibiliographicType",
}

# BibliographyDataType constants (book=1 matches Insert → Bibliographic Entry).
_BIB_TYPE_NAMES = {
    "article": 0,
    "book": 1,
    "booklet": 2,
    "conference": 3,
    "inbook": 4,
    "incollection": 5,
    "inproceedings": 6,
    "journal": 7,
    "manual": 8,
    "mastersthesis": 9,
    "misc": 10,
    "phdthesis": 11,
    "proceedings": 12,
    "techreport": 13,
    "unpublished": 14,
    "email": 15,
    "www": 16,
}

_BIB_CITE_SERVICE = "com.sun.star.text.textfield.Bibliography"
_BIB_CITE_SERVICE_ALT = "com.sun.star.text.TextField.Bibliography"
_IGNORED_CITE_KWARGS = frozenset({"zotero_key", "citekey", "locator", "csl_style"})


def canonicalize_bibliography_field_name(name: str) -> str | None:
    """Map a model/IDL alias to the live Fields PropertyValue.Name, or None."""
    raw = (name or "").strip()
    if not raw:
        return None
    if raw in _BIB_FIELD_NAME_SET:
        return raw
    folded = raw.replace("-", "_")
    alias = _BIB_FIELD_ALIASES.get(folded.lower())
    if alias:
        return alias
    # IDENTIFIER → Identifier, BIBILIOGRAPHIC_TYPE → BibiliographicType
    if "_" in folded:
        parts = [p for p in folded.split("_") if p]
        pretty = "".join(p[:1].upper() + p[1:].lower() for p in parts)
        if pretty == "BibilographicType":
            pretty = "BibiliographicType"
        if pretty in _BIB_FIELD_NAME_SET:
            return pretty
    titled = raw[:1].upper() + raw[1:] if raw else raw
    if titled in _BIB_FIELD_NAME_SET:
        return titled
    return None


def resolve_bibliographic_type(value: Any) -> int | None:
    """Coerce a type hint to BibliographyDataType (int). None if unused/invalid."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return int(text)
    return _BIB_TYPE_NAMES.get(text.lower().replace(" ", "").replace("-", ""))


def collect_bibliography_field_pairs(kwargs: dict[str, Any]) -> list[tuple[str, Any]]:
    """Build (Name, Value) pairs for Fields. Later keys override earlier ones."""
    pairs: dict[str, Any] = {}
    extra = kwargs.get("fields")
    if isinstance(extra, dict):
        for key, value in extra.items():
            canon = canonicalize_bibliography_field_name(str(key))
            if canon is None or value is None:
                continue
            pairs[canon] = value

    convenience = (
        ("identifier", kwargs.get("identifier")),
        ("author", kwargs.get("author")),
        ("title", kwargs.get("title")),
        ("year", kwargs.get("year")),
        ("pages", kwargs.get("pages")),
    )
    for alias, value in convenience:
        if value is None or value == "":
            continue
        pairs[_BIB_FIELD_ALIASES[alias]] = value

    type_val = resolve_bibliographic_type(
        kwargs.get("bibliographic_type", kwargs.get("bibiliographic_type"))
    )
    if type_val is not None:
        pairs["BibiliographicType"] = type_val

    if "Identifier" not in pairs or pairs["Identifier"] in (None, ""):
        text = kwargs.get("text")
        if text not in (None, ""):
            pairs["Identifier"] = text

    out: list[tuple[str, Any]] = []
    for name, value in pairs.items():
        if value is None:
            continue
        if name == "BibiliographicType":
            resolved = resolve_bibliographic_type(value)
            if resolved is None:
                continue
            out.append((name, resolved))
            continue
        # Year and the rest of the bag are strings in SwAuthorityField::QueryValue.
        out.append((name, str(value)))
    return out


def fields_sequence_to_dict(raw: Any) -> dict[str, Any]:
    """Map a Fields PropertyValue sequence to {Name: Value} (non-empty only)."""
    result: dict[str, Any] = {}
    if not raw:
        return result
    try:
        items = list(raw)
    except TypeError:
        return result
    for item in items:
        name = getattr(item, "Name", None)
        if not name:
            continue
        value = getattr(item, "Value", None)
        if value in (None, ""):
            continue
        result[str(name)] = value
    return result


def index_kind_from_uno(idx: Any) -> str:
    """Prefer getServiceName(); fall back to the old SwX* remaps."""
    service = None
    if hasattr(idx, "getServiceName"):
        try:
            service = idx.getServiceName()
        except Exception:
            service = None
    if isinstance(service, str) and service in _INDEX_SERVICE_TO_KIND:
        return _INDEX_SERVICE_TO_KIND[service]

    impl = None
    if hasattr(idx, "getImplementationName"):
        try:
            impl = idx.getImplementationName()
        except Exception:
            impl = None
    if isinstance(impl, str) and impl in _INDEX_IMPL_TO_KIND:
        return _INDEX_IMPL_TO_KIND[impl]
    if isinstance(impl, str) and impl:
        return impl
    if isinstance(service, str) and service:
        return service
    return "unknown"


def is_bibliography_text_field(field: Any) -> bool:
    """True for native Writer bibliography cite fields (not the index table)."""
    if field is None:
        return False
    if hasattr(field, "supportsService"):
        for service in (_BIB_CITE_SERVICE, _BIB_CITE_SERVICE_ALT):
            try:
                if field.supportsService(service):
                    return True
            except Exception:
                continue
    return False


def _create_property_value(name: str, value: Any) -> Any:
    import uno

    prop = cast("Any", uno.createUnoStruct("com.sun.star.beans.PropertyValue"))
    prop.Name = name
    prop.Value = value
    return prop


def set_bibliography_field_values(field: Any, pairs: list[tuple[str, Any]]) -> None:
    """Write Fields as a typed UNO sequence.

    A Python tuple passed to ``setPropertyValue("Fields", …)`` is accepted and
    silently dropped. ``uno.Any("[]com.sun.star.beans.PropertyValue", seq)``
    must go through ``uno.invoke`` (same pattern as CustomShapeGeometry).
    Set this on the descriptor *before* insert so attach() applies aPropSeq.
    """
    import uno

    uno_any = getattr(uno, "Any")
    seq = tuple(_create_property_value(name, value) for name, value in pairs)
    typed = uno_any("[]com.sun.star.beans.PropertyValue", cast("Any", seq))
    uno.invoke(field, "setPropertyValue", cast("Any", ("Fields", typed)))


class IndexesUpdateAll(ToolWriterIndexBase):
    name: str | None = "indexes_update_all"
    intent: str | None = "navigate"
    description: str = (
        "Refresh all document indexes (TOC, alphabetical, bibliography table). "
        "Call after inserting or editing bibliography cites so the reference list updates. "
        "A TOC refresh rebuilds every entry and drops customized direct formatting. "
        "To change one entry without that rebuild, use indexes_refresh_toc_entry, "
        "indexes_insert_toc_entry, or indexes_delete_toc_entry. "
        "Those tools do not call update() and do not recalculate page numbers."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        if not hasattr(doc, "getDocumentIndexes"):
            return self._tool_error("Document does not support indexes")
        indexes = doc.getDocumentIndexes()
        count = indexes.getCount()
        refreshed = []
        for i in range(count):
            idx = indexes.getByIndex(i)
            idx.update()
            name = idx.getName() if hasattr(idx, "getName") else "index_%d" % i
            refreshed.append(name)
        return {"status": "ok", "refreshed": refreshed, "count": count}


def _contained(text: Any, outer: Any, inner: Any) -> bool:
    """True when *inner* lies entirely inside *outer*."""
    try:
        start_ok = int(text.compareRegionStarts(outer.getStart(), inner.getStart())) >= 0
        end_ok = int(text.compareRegionEnds(inner.getEnd(), outer.getEnd())) != -1
        return start_ok and end_ok
    except Exception:
        return False


def _set_protected(idx: Any, value: bool) -> None:
    if hasattr(idx, "setPropertyValue"):
        idx.setPropertyValue("IsProtected", value)
        return
    idx.IsProtected = value


def _entry_before(found: Any, content: str) -> tuple[str, str]:
    """The entry paragraph, and that paragraph with this one match replaced.

    ``found.getString()`` is only the matched substring. The entry is the
    paragraph, so the page number and the rest of the line stay in the report.
    """
    try:
        text = found.getText()
        origin = text.createTextCursorByRange(found.getStart())
        origin.gotoStartOfParagraph(False)
        prefix = text.createTextCursorByRange(origin.getStart())
        prefix.gotoRange(found.getStart(), True)
        offset = len(prefix.getString() or "")
        para = text.createTextCursorByRange(origin.getStart())
        para.gotoEndOfParagraph(True)
        entry = para.getString() or ""
        matched = found.getString() or ""
    except Exception:
        return "", ""
    end = offset + len(matched)
    if entry[offset:end] != matched:
        end = min(len(entry), end)
    after = entry[:offset] + content + entry[end:]
    return entry[:160], after[:160]


def _paragraph_string(cursor: Any) -> str:
    """Visible text of the paragraph that contains *cursor*."""
    if cursor is None:
        return ""
    try:
        text = cursor.getText()
        para = text.createTextCursorByRange(cursor.getStart())
        para.gotoStartOfParagraph(False)
        para.gotoEndOfParagraph(True)
        return (para.getString() or "")[:160]
    except Exception:
        return ""


def _enum_has_more(enum: Any) -> bool:
    """True only when ``hasMoreElements()`` is True.

    ``if not enum.hasMoreElements()`` never stops on pytest MagicMock (the
    return is another mock). Same rule as ``hyperlink_fixup._enum_has_more``.
    """
    try:
        return enum.hasMoreElements() is True
    except Exception:
        return False


def resolve_toc(doc: Any, index: Any) -> tuple[Any | None, str | None]:
    """The single TOC, or the TOC at document-index *index*. Error string otherwise."""
    if not hasattr(doc, "getDocumentIndexes"):
        return None, "Document does not support indexes."
    indexes = doc.getDocumentIndexes()
    count = indexes.getCount()
    if index is None:
        tocs = []
        for i in range(count):
            idx = indexes.getByIndex(i)
            if index_kind_from_uno(idx) == "toc":
                tocs.append(idx)
        if len(tocs) != 1:
            return None, "index is required when the document does not have exactly one table of contents."
        return tocs[0], None
    if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= count:
        return None, "index must be a document index position from indexes_list."
    idx = indexes.getByIndex(index)
    if index_kind_from_uno(idx) != "toc":
        return None, "index is not a table of contents."
    return idx, None


def toc_match(doc: Any, anchor: Any, old_content: str, occurrence: Any) -> tuple[Any | None, str | None]:
    """The *occurrence* search hit that lies inside the TOC anchor."""
    from .. import search as search_mod

    ranges = search_mod.find_all_ranges(doc, old_content) or []
    text = anchor.getText()
    inside = [found for found in ranges if _contained(text, anchor, found)]
    pick = 0 if occurrence is None else occurrence
    if not inside or pick >= len(inside):
        if occurrence not in (None, 0):
            return None, "occurrence is past the last table-of-contents match."
        return None, "No table-of-contents entry contains that text."
    return inside[pick], None


def _commit_toc_mutation(
    tool: Any,
    doc: Any,
    idx: Any,
    mutate: Callable[[], dict[str, Any] | None],
    *,
    failure_message: str,
    failure_code: str = "TOOL_EXECUTION_ERROR",
    fallback_message: str = "TOC entry update failed.",
) -> dict[str, Any] | None:
    """Unprotect one TOC, run *mutate* in one undo context, then protect again.

    A default content index is protected. These one-entry edits must not call
    ``update()`` (that rebuilds every row and drops direct formatting). Clearing
    ``IsProtected`` only for the edit, then restoring it, keeps the flag the
    document had. On failure the undo title is undone so neighbors stay put.
    *mutate* returns an error payload to roll back, or None on success.
    """
    from ..edit_review import next_agent_edit_undo_title

    try:
        was_protected = bool(idx.getPropertyValue("IsProtected"))
    except Exception:
        was_protected = bool(getattr(idx, "IsProtected", False))
    try:
        mgr = doc.getUndoManager()
        if mgr is None or mgr.isLocked():
            raise RuntimeError("undo manager is locked")
        undo_title = next_agent_edit_undo_title()
        mgr.enterUndoContext(undo_title)
    except Exception:
        return tool._tool_error(
            "Cannot edit the TOC entry atomically (no usable undo context).",
            code="UNDO_UNAVAILABLE")
    applied = False
    error: dict[str, Any] | None = None
    try:
        if was_protected:
            _set_protected(idx, False)
        rejected = mutate()
        if isinstance(rejected, dict):
            error = rejected
        else:
            applied = True
    except Exception as exc:
        error = tool._tool_error(failure_message % exc, code=failure_code)
    finally:
        if was_protected:
            try:
                _set_protected(idx, True)
            except Exception:
                applied = False
                if error is None:
                    error = tool._tool_error(
                        "Could not restore table-of-contents protection; the edit was rolled back.")
        left = False
        try:
            mgr.leaveUndoContext()
            left = True
        except Exception:
            applied = False
            if error is None:
                error = tool._tool_error("Could not close the TOC edit undo context.")
        if not applied and left:
            try:
                titles = mgr.getAllUndoActionTitles()
                if titles and titles[0] == undo_title:
                    mgr.undo()
            except Exception:
                pass
    if error is not None or not applied:
        if error is not None:
            return error
        return tool._tool_error(fallback_message)
    return None


def _invalid_occurrence(occurrence: Any) -> str | None:
    if occurrence is not None and (isinstance(occurrence, bool) or not isinstance(occurrence, int) or occurrence < 0):
        return "occurrence must be a non-negative integer."
    return None


def _reject_markup(text: str) -> bool:
    from ..format import content_has_markup
    return content_has_markup(text)


# Generated TOC rows use paragraph styles "Contents 1" … "Contents 10"
# (the title is "Contents Heading"). Probed on LibreOffice: the page number
# is plain digits in the same text portion as the title ("Alpha title\\t1"),
# not a page field. The outline or bookmark URL covers that whole portion.
_CONTENTS_LEVEL_PREFIX = "Contents "


def _is_contents_level_style(style: str) -> bool:
    if not style.startswith(_CONTENTS_LEVEL_PREFIX):
        return False
    tail = style[len(_CONTENTS_LEVEL_PREFIX):]
    if not tail.isdigit():
        return False
    level = int(tail)
    return 1 <= level <= 10


def _para_style_name(para: Any) -> str:
    try:
        return str(para.getPropertyValue("ParaStyleName") or "")
    except Exception:
        return ""


def _paragraph_style_exists(doc: Any, name: str) -> bool:
    try:
        styles = doc.getStyleFamilies().getByName("ParagraphStyles")
        return bool(styles.hasByName(name))
    except Exception:
        return False


def _paragraphs_in_anchor(anchor: Any) -> list[Any]:
    """Paragraphs of the document text that lie inside the TOC anchor."""
    text = anchor.getText()
    try:
        enum = text.createEnumeration()
    except Exception:
        return []
    rows: list[Any] = []
    while _enum_has_more(enum):
        para = enum.nextElement()
        if _contained(text, anchor, para):
            rows.append(para)
    return rows


def _paragraph_element(found: Any) -> Any | None:
    """The paragraph object that contains the search hit *found*."""
    try:
        text = found.getText()
        enum = text.createEnumeration()
    except Exception:
        return None
    while _enum_has_more(enum):
        para = enum.nextElement()
        if _contained(text, para, found):
            return para
    return None


def _entry_text(para: Any) -> str:
    try:
        value = para.getString() or ""
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def _compose_toc_line(content: str, page: str | None, sibling_text: str) -> tuple[str, str, bool]:
    """One TOC line, the page text used, and whether that page came from the sibling.

    *content* may already be ``Title\\tpage``. Otherwise the sibling's tab and
    page digits are appended when the sibling has them. Omit *page* to copy
    those digits; pass *page* to write that text instead.
    """
    if "\t" in content:
        _title, explicit = content.rsplit("\t", 1)
        return content, explicit, False
    sibling_page = None
    if "\t" in sibling_text:
        sibling_page = sibling_text.rsplit("\t", 1)[1]
    if page is None:
        if sibling_page is None:
            return content, "", False
        return content + "\t" + sibling_page, sibling_page, True
    return content + "\t" + page, page, False


_CLONED_CHAR_PROPS = (
    "CharColor",
    "CharUnderline",
    "CharWeight",
    "CharPosture",
    "CharFontName",
    "CharHeight",
    "CharStyleName",
    "CharBackColor",
)


def _char_props(para: Any) -> dict[str, Any]:
    """Character properties of the sibling row's first text portion.

    Written back on the same cursor that sets ``HyperLinkURL``. Assigning the
    URL paints Internet-link defaults (navy + single underline); the sibling's
    colour and underline have to go on that cursor or the new row will not
    match a customized TOC.
    """
    props: dict[str, Any] = {}
    try:
        portions = para.createEnumeration()
    except Exception:
        return props
    while _enum_has_more(portions):
        portion = portions.nextElement()
        try:
            if portion.getPropertyValue("TextPortionType") != "Text":
                continue
            chunk = portion.getString() or ""
        except Exception:
            continue
        if not isinstance(chunk, str) or not chunk:
            continue
        for name in _CLONED_CHAR_PROPS:
            try:
                value = portion.getPropertyValue(name)
            except Exception:
                continue
            if value is None:
                continue
            props[name] = value
        break
    return props


def _snapshot_tab_stops(para: Any) -> tuple[Any, ...]:
    """Copy of *para*'s tab stops as fresh TabStop structs.

    The live sequence cannot be written back: a Python tuple of those structs
    is refused with IllegalArgumentException, and the objects themselves do not
    round-trip through ``uno.Any``. ``styles._set_style_property`` builds new
    structs and passes ``[]com.sun.star.style.TabStop`` via ``uno.invoke``.
    """
    try:
        tabs = list(para.getPropertyValue("ParaTabStops") or ())
    except Exception:
        return ()
    if not tabs:
        return ()
    import uno

    built = []
    for src in tabs:
        stop = cast("Any", uno.createUnoStruct("com.sun.star.style.TabStop"))
        stop.Position = src.Position
        stop.Alignment = src.Alignment
        stop.FillChar = src.FillChar
        stop.DecimalChar = src.DecimalChar
        built.append(stop)
    return tuple(built)


def _apply_tab_stops(cursor: Any, stops: tuple[Any, ...]) -> None:
    import uno

    uno_rt = cast("Any", uno)
    uno_rt.invoke(
        cursor,
        "setPropertyValue",
        ("ParaTabStops", uno_rt.Any("[]com.sun.star.style.TabStop", stops)),
    )


def _paragraph_break() -> int:
    import uno

    try:
        raw = cast("Any", uno).getConstantByName(
            "com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK")
    except Exception:
        return 0
    # 0 is PARAGRAPH_BREAK. The constant is an int; anything else falls back.
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    return 0


def _select_paragraph(cursor: Any) -> Any:
    text = cursor.getText()
    para = text.createTextCursorByRange(cursor.getStart())
    para.gotoStartOfParagraph(False)
    para.gotoEndOfParagraph(True)
    return para


def _insert_toc_paragraph(
    text: Any,
    ref_para: Any,
    where: str,
    new_text: str,
    style_name: str,
    tab_stops: tuple[Any, ...] | None,
    url: str | None,
    char_props: dict[str, Any],
) -> str:
    """Insert one paragraph before or after *ref_para*. Return its text.

    Inserting a break at the start of *ref_para* leaves the cursor in the
    original row. ``gotoPreviousParagraph`` steps into the new empty paragraph
    so the neighbor's text is not prefixed. A break at the end already leaves
    the cursor in the new paragraph (probed on LibreOffice).
    """
    brk = _paragraph_break()
    if where == "before":
        cursor = text.createTextCursorByRange(ref_para.getStart())
        cursor.gotoStartOfParagraph(False)
        text.insertControlCharacter(cursor, brk, False)
        if cursor.gotoPreviousParagraph(False) is not True:
            raise RuntimeError("could not reach the inserted TOC paragraph")
    else:
        cursor = text.createTextCursorByRange(ref_para.getEnd())
        cursor.gotoEndOfParagraph(False)
        text.insertControlCharacter(cursor, brk, False)
    text.insertString(cursor, new_text, False)
    para = _select_paragraph(cursor)
    para.setPropertyValue("ParaStyleName", style_name)
    # After the style, so a direct tab override is not reset by Contents N.
    if tab_stops:
        _apply_tab_stops(para, tab_stops)
    # Style and tab writes can drop the selection. Re-select, then set the URL
    # and the sibling's Char* on that same cursor. A later cursor does not
    # undo the Internet-link colour and underline that HyperLinkURL paints.
    para = _select_paragraph(para)
    if url:
        para.setPropertyValue("HyperLinkURL", url)
    for name, value in char_props.items():
        try:
            para.setPropertyValue(name, value)
        except Exception:
            continue
    lived = para.getString() or ""
    return lived if isinstance(lived, str) else new_text


def _remove_paragraph(text: Any, para: Any) -> None:
    """Delete *para* including its paragraph break, leaving the next row intact.

    Select from this paragraph start to the next paragraph start so the range
    is the row plus the break, not the next row's first character
    (``goRight(1)`` after ``gotoEndOfParagraph`` is version-fragile; same
    approach as ``notebook_runner._delete_paragraph_at``). The last paragraph
    of the text has no next row, so the preceding break is removed instead of
    leaving an empty paragraph behind.
    """
    sel = text.createTextCursorByRange(para.getStart())
    sel.gotoStartOfParagraph(False)
    nxt = text.createTextCursorByRange(sel.getStart())
    if nxt.gotoNextParagraph(False) is True:
        nxt.gotoStartOfParagraph(False)
        sel.gotoRange(nxt.getStart(), True)
        sel.setString("")
        return
    prev = text.createTextCursorByRange(sel.getStart())
    if prev.gotoPreviousParagraph(False) is True:
        prev.gotoEndOfParagraph(False)
        end = text.createTextCursorByRange(sel.getStart())
        end.gotoEndOfParagraph(False)
        prev.gotoRange(end.getEnd(), True)
        prev.setString("")
        return
    sel.gotoEndOfParagraph(True)
    sel.setString("")


class IndexesRefreshTocEntry(ToolWriterIndexBase):
    name: str | None = "indexes_refresh_toc_entry"
    intent: str | None = "edit"
    description: str = (
        "Replace one substring inside a single table-of-contents entry and, when that "
        "text sits in one outline hyperlink (#…|outline), update that URL. "
        "Does not call index update(), so other entries, tabs, page numbers, and direct "
        "formatting stay. Page numbers are left as they are. "
        "indexes_update_all is the full rebuild and drops customized TOC formatting. "
        "Pass hyperlink_url to set the outline target, including when content equals "
        "old_content. Bookmark targets are not rewritten. "
        "To add or remove one row, use indexes_insert_toc_entry or indexes_delete_toc_entry."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "old_content": {"type": "string", "description": "Substring to find inside the TOC entry."},
            "content": {"type": "string", "description": "Plain text to write in its place. May equal old_content when only hyperlink_url should change."},
            "hyperlink_url": {"type": "string", "description": "Exact outline URL (#…|outline) when the match overlaps exactly one outline link. Omit to substitute old_content once outside the |outline suffix."},
            "index": {"type": "integer", "minimum": 0, "description": "Document index position from indexes_list. Omit when the document has exactly one TOC."},
            "occurrence": {"type": "integer", "minimum": 0, "description": "0-based match inside the TOC only. Omit for the first. Body text with the same words is not a match."},
            "dry_run": {"type": "boolean", "description": "Do not edit. Report text (the entry before) and text_after, plus the outline URL before and after when the match is one |outline link."},
        },
        "required": ["old_content", "content"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        old_content = kwargs.get("old_content")
        content = kwargs.get("content")
        if not isinstance(old_content, str) or not str(old_content).strip():
            return self._tool_error("old_content must be a non-empty string.", code="INVALID_PARAM")
        if not isinstance(content, str):
            return self._tool_error("content must be plain text.", code="INVALID_PARAM")
        from ..format import content_has_markup
        if content_has_markup(content):
            return self._tool_error(
                "indexes_refresh_toc_entry takes plain text so the entry's formatting stays.",
                code="INVALID_PARAM")
        raw_url = kwargs.get("hyperlink_url")
        override = None
        if raw_url is not None:
            if not isinstance(raw_url, str) or not raw_url.strip():
                return self._tool_error("hyperlink_url must be a non-empty string.", code="INVALID_PARAM")
            override = raw_url
        occurrence = kwargs.get("occurrence")
        if occurrence is not None and (isinstance(occurrence, bool) or not isinstance(occurrence, int) or occurrence < 0):
            return self._tool_error("occurrence must be a non-negative integer.", code="INVALID_PARAM")
        index = kwargs.get("index")
        idx, index_error = self._resolve_toc(doc, index)
        if index_error or idx is None:
            return self._tool_error(
                index_error or "Could not find the table of contents.", code="INVALID_PARAM")
        try:
            anchor = idx.getAnchor()
        except Exception:
            return self._tool_error("Could not read the table of contents.", code="TOOL_EXECUTION_ERROR")
        found, find_error = self._toc_match(doc, anchor, str(old_content).strip(), occurrence)
        if find_error:
            return self._tool_error(find_error, code="NOT_FOUND")
        preview, preview_error = self._preview(found, content, override)
        if preview_error or preview is None:
            return preview_error or self._tool_error(
                "Could not preview the TOC entry.", code="TOOL_EXECUTION_ERROR")
        if kwargs.get("dry_run"):
            preview["status"] = "ok"
            preview["dry_run"] = True
            return preview
        return self._write(ctx, doc, idx, found, content, override, preview)

    def _resolve_toc(self, doc: Any, index: Any) -> tuple[Any | None, str | None]:
        return resolve_toc(doc, index)

    def _toc_match(self, doc: Any, anchor: Any, old_content: str, occurrence: Any) -> tuple[Any | None, str | None]:
        return toc_match(doc, anchor, old_content, occurrence)

    def _preview(self, found: Any, content: str, override: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        from ..hyperlink_fixup import (
            capture_outline_hyperlinks,
            empty_snapshot,
            plan_outline_updates,
            public_hyperlink_reports,
        )

        try:
            snapshot = capture_outline_hyperlinks(found)
        except Exception:
            snapshot = empty_snapshot()
        if override and len(snapshot.links) != 1:
            return None, self._tool_error(
                "hyperlink_url applies only when the match overlaps exactly one outline hyperlink (|outline).",
                code="INVALID_PARAM")
        plans = plan_outline_updates(snapshot.links, snapshot.matched, content, override)
        reports = public_hyperlink_reports(plans)
        before, after = _entry_before(found, content)
        preview: dict[str, Any] = {"text": before, "text_after": after}
        if len(reports) == 1:
            preview["hyperlink_url"] = reports[0]["hyperlink_url"]
            preview["hyperlink_url_after"] = reports[0]["hyperlink_url_after"]
            preview["hyperlink_updated"] = reports[0]["hyperlink_updated"]
        elif reports:
            preview["hyperlinks"] = reports
        return preview, None

    def _write(self, ctx: Any, doc: Any, idx: Any, found: Any, content: str, override: Any, preview: Any) -> dict[str, Any]:
        from ..edit_review import collapsed_anchor
        from ..format import replace_preserving_format
        from ..hyperlink_fixup import (
            capture_outline_hyperlinks,
            empty_snapshot,
            restore_outline_hyperlinks,
        )

        def mutate() -> dict[str, Any] | None:
            try:
                snapshot = capture_outline_hyperlinks(found)
            except Exception:
                snapshot = empty_snapshot()
            if override and len(snapshot.links) != 1:
                return self._tool_error(
                    "hyperlink_url applies only when the match overlaps exactly one outline hyperlink (|outline).",
                    code="INVALID_PARAM")
            point = collapsed_anchor(found)
            replace_preserving_format(
                doc, found, content, ctx.ctx, in_undo_context=True, split_author=False)
            lived = _paragraph_string(point)
            if lived:
                preview["text_after"] = lived
            if snapshot.links or snapshot.preserve_url:
                reports = restore_outline_hyperlinks(point, snapshot, content, override)
                if len(reports) == 1:
                    preview["hyperlink_url"] = reports[0]["hyperlink_url"]
                    preview["hyperlink_url_after"] = reports[0]["hyperlink_url_after"]
                    preview["hyperlink_updated"] = reports[0]["hyperlink_updated"]
            return None

        error = _commit_toc_mutation(
            self,
            doc,
            idx,
            mutate,
            failure_message="TOC entry update failed; the change was rolled back (%s).",
            failure_code="HYPERLINK_UPDATE_FAILED",
        )
        if error is not None:
            return error
        preview["status"] = "ok"
        preview["dry_run"] = False
        return preview


class IndexesDeleteTocEntry(ToolWriterIndexBase):
    name: str | None = "indexes_delete_toc_entry"
    intent: str | None = "edit"
    description: str = (
        "Delete one table-of-contents entry (the whole paragraph). "
        "Identify it the same way as indexes_refresh_toc_entry: old_content matched "
        "inside the TOC only, plus optional index and occurrence. "
        "Does not call index update(), so neighboring entries, tabs, and direct formatting stay. "
        "indexes_update_all is the full rebuild and drops customized TOC formatting. "
        "The agent decides which row is obsolete; this tool does not compare the outline."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "old_content": {"type": "string", "description": "Plain text to find inside the TOC entry to delete."},
            "index": {"type": "integer", "minimum": 0, "description": "Document index position from indexes_list. Omit when the document has exactly one TOC."},
            "occurrence": {"type": "integer", "minimum": 0, "description": "0-based match inside the TOC only. Omit for the first. Body text with the same words is not a match."},
            "dry_run": {"type": "boolean", "description": "Do not edit. Report text (the entry that would be removed) and an empty text_after."},
        },
        "required": ["old_content"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        old_content = kwargs.get("old_content")
        if not isinstance(old_content, str) or not str(old_content).strip():
            return self._tool_error("old_content must be a non-empty string.", code="INVALID_PARAM")
        if _reject_markup(old_content):
            return self._tool_error(
                "indexes_delete_toc_entry takes plain text so the entry match stays a substring.",
                code="INVALID_PARAM")
        occurrence_error = _invalid_occurrence(kwargs.get("occurrence"))
        if occurrence_error:
            return self._tool_error(occurrence_error, code="INVALID_PARAM")
        index = kwargs.get("index")
        idx, index_error = resolve_toc(doc, index)
        if index_error or idx is None:
            return self._tool_error(
                index_error or "Could not find the table of contents.", code="INVALID_PARAM")
        try:
            anchor = idx.getAnchor()
        except Exception:
            return self._tool_error("Could not read the table of contents.", code="TOOL_EXECUTION_ERROR")
        found, find_error = toc_match(doc, anchor, str(old_content).strip(), kwargs.get("occurrence"))
        if find_error or found is None:
            return self._tool_error(find_error or "No table-of-contents entry contains that text.", code="NOT_FOUND")
        para = _paragraph_element(found)
        if para is None:
            return self._tool_error("Could not read the TOC entry.", code="TOOL_EXECUTION_ERROR")
        entry = _entry_text(para)[:160]
        preview: dict[str, Any] = {"text": entry, "text_after": ""}
        if kwargs.get("dry_run"):
            preview["status"] = "ok"
            preview["dry_run"] = True
            return preview

        def mutate() -> dict[str, Any] | None:
            _remove_paragraph(anchor.getText(), para)
            return None

        error = _commit_toc_mutation(
            self,
            doc,
            idx,
            mutate,
            failure_message="TOC entry delete failed; the change was rolled back (%s).",
            fallback_message="TOC entry delete failed.",
        )
        if error is not None:
            return error
        preview["status"] = "ok"
        preview["dry_run"] = False
        return preview


class IndexesInsertTocEntry(ToolWriterIndexBase):
    name: str | None = "indexes_insert_toc_entry"
    intent: str | None = "edit"
    description: str = (
        "Insert one new row into an existing table of contents. "
        "Does not call index update(), and does not modify neighboring entries. "
        "Clones the sibling row's Contents N paragraph style, direct character formatting, "
        "and tab stops. Set hyperlink_url to an outline target (#…|outline). "
        "Page numbers follow the sibling row: plain text after a tab (generated TOC rows "
        "store digits in the entry, not a page field). Pass page to set that text; omit it "
        "to copy the sibling's page text. "
        "position is before, after (both need old_content), or end (after the last TOC entry). "
        "indexes_update_all is the full rebuild and drops customized TOC formatting. "
        "The agent decides what is missing; this tool does not sync the outline."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Plain text of the new entry title. May be the full line (Title followed by a tab and the page) when page is omitted."},
            "page": {"type": "string", "description": "Plain page text written after a tab. Omit to copy the sibling row's page text. Not a page-number field."},
            "hyperlink_url": {"type": "string", "description": "Outline target (#…|outline) for the new row only. Omit to leave the new row unlinked."},
            "position": {"type": "string", "enum": ["before", "after", "end"], "description": "Where to insert the one row. before/after need old_content. end appends after the last TOC entry. Default end."},
            "old_content": {"type": "string", "description": "Plain text of the existing TOC entry to insert before or after. Not used when position is end."},
            "level": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Contents N paragraph style (1-10). Omit to clone the sibling entry's style."},
            "index": {"type": "integer", "minimum": 0, "description": "Document index position from indexes_list. Omit when the document has exactly one TOC."},
            "occurrence": {"type": "integer", "minimum": 0, "description": "0-based match inside the TOC only, when position is before or after. Omit for the first."},
            "dry_run": {"type": "boolean", "description": "Do not edit. Report text (the sibling entry) and text_after (the row that would be inserted)."},
        },
        "required": ["content"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        content = kwargs.get("content")
        if not isinstance(content, str) or not content.strip():
            return self._tool_error("content must be a non-empty string.", code="INVALID_PARAM")
        content = content.strip()
        if _reject_markup(content):
            return self._tool_error(
                "indexes_insert_toc_entry takes plain text so the new entry's formatting can be cloned.",
                code="INVALID_PARAM")
        page, page_error = self._page_text(kwargs.get("page"))
        if page_error:
            return self._tool_error(page_error, code="INVALID_PARAM")
        if page is not None and _reject_markup(page):
            return self._tool_error("page must be plain text.", code="INVALID_PARAM")
        if page is not None and "\t" in content:
            return self._tool_error(
                "Pass either a full line in content (title, tab, page) or a separate page, not both.",
                code="INVALID_PARAM")
        url, url_error = self._outline_url(kwargs.get("hyperlink_url"))
        if url_error:
            return self._tool_error(url_error, code="INVALID_PARAM")
        position = kwargs.get("position") or "end"
        if position not in ("before", "after", "end"):
            return self._tool_error("position must be before, after, or end.", code="INVALID_PARAM")
        old_content = kwargs.get("old_content")
        occurrence = kwargs.get("occurrence")
        if position == "end":
            if old_content not in (None, ""):
                return self._tool_error(
                    "old_content is only used when position is before or after.",
                    code="INVALID_PARAM")
            if occurrence is not None:
                return self._tool_error(
                    "occurrence is only used when position is before or after.",
                    code="INVALID_PARAM")
        else:
            if not isinstance(old_content, str) or not old_content.strip():
                return self._tool_error(
                    "old_content is required when position is before or after.",
                    code="INVALID_PARAM")
            if _reject_markup(old_content):
                return self._tool_error(
                    "old_content must be plain text.",
                    code="INVALID_PARAM")
        occurrence_error = _invalid_occurrence(occurrence)
        if occurrence_error:
            return self._tool_error(occurrence_error, code="INVALID_PARAM")
        level, level_error = self._level(kwargs.get("level"))
        if level_error:
            return self._tool_error(level_error, code="INVALID_PARAM")
        index = kwargs.get("index")
        idx, index_error = resolve_toc(doc, index)
        if index_error or idx is None:
            return self._tool_error(
                index_error or "Could not find the table of contents.", code="INVALID_PARAM")
        try:
            anchor = idx.getAnchor()
        except Exception:
            return self._tool_error("Could not read the table of contents.", code="TOOL_EXECUTION_ERROR")
        paragraphs = _paragraphs_in_anchor(anchor)
        if position == "end":
            ref = self._end_anchor(paragraphs)
        else:
            found, find_error = toc_match(doc, anchor, str(old_content).strip(), occurrence)
            if find_error or found is None:
                return self._tool_error(
                    find_error or "No table-of-contents entry contains that text.", code="NOT_FOUND")
            ref = _paragraph_element(found)
        if ref is None:
            return self._tool_error(
                "The table of contents has no paragraph to insert beside.",
                code="TOOL_EXECUTION_ERROR")
        style_name, style_error = self._style_name(doc, ref, level)
        if style_error or not style_name:
            return self._tool_error(
                style_error or "Could not choose a TOC paragraph style.",
                code="INVALID_PARAM")
        sibling_text = _entry_text(ref)
        line, page_used, page_from_sibling = _compose_toc_line(content, page, sibling_text)
        copy_tabs = level is None and _is_contents_level_style(_para_style_name(ref))
        # Snapshot before the edit. The sibling paragraph object is not stable
        # once a row is inserted beside it.
        tab_stops = _snapshot_tab_stops(ref) if copy_tabs else None
        char_props = _char_props(ref) if _is_contents_level_style(_para_style_name(ref)) else {}
        where = "after" if position == "end" else position
        preview: dict[str, Any] = {
            "text": sibling_text[:160],
            "text_after": line[:160],
            "position": position,
            "para_style": style_name,
            "page": page_used,
            "page_from_sibling": page_from_sibling,
        }
        if url:
            preview["hyperlink_url"] = url
        if kwargs.get("dry_run"):
            preview["status"] = "ok"
            preview["dry_run"] = True
            return preview

        def mutate() -> dict[str, Any] | None:
            lived = _insert_toc_paragraph(
                anchor.getText(), ref, where, line, style_name, tab_stops, url, char_props)
            if lived:
                preview["text_after"] = lived[:160]
            return None

        error = _commit_toc_mutation(
            self,
            doc,
            idx,
            mutate,
            failure_message="TOC entry insert failed; the change was rolled back (%s).",
            fallback_message="TOC entry insert failed.",
        )
        if error is not None:
            return error
        preview["status"] = "ok"
        preview["dry_run"] = False
        return preview

    def _page_text(self, raw: Any) -> tuple[str | None, str | None]:
        if raw is None:
            return None, None
        if isinstance(raw, bool) or isinstance(raw, float):
            return None, "page must be plain text (or an integer)."
        if isinstance(raw, int):
            return str(raw), None
        if not isinstance(raw, str) or not raw.strip():
            return None, "page must be plain text."
        return raw.strip(), None

    def _outline_url(self, raw: Any) -> tuple[str | None, str | None]:
        if raw is None:
            return None, None
        if not isinstance(raw, str) or not raw.strip():
            return None, "hyperlink_url must be a non-empty string."
        url = raw.strip()
        if not url.startswith("#") or "|outline" not in url:
            return None, "hyperlink_url must be an outline target (#…|outline)."
        return url, None

    def _level(self, raw: Any) -> tuple[int | None, str | None]:
        if raw is None:
            return None, None
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1 or raw > 10:
            return None, "level must be an integer from 1 to 10."
        return raw, None

    def _end_anchor(self, paragraphs: list[Any]) -> Any | None:
        entries = [para for para in paragraphs if _is_contents_level_style(_para_style_name(para))]
        if entries:
            return entries[-1]
        if paragraphs:
            return paragraphs[-1]
        return None

    def _style_name(self, doc: Any, ref: Any, level: int | None) -> tuple[str | None, str | None]:
        if level is not None:
            name = "Contents %d" % level
            if not _paragraph_style_exists(doc, name):
                return None, "Paragraph style %s was not found." % name
            return name, None
        style = _para_style_name(ref)
        if _is_contents_level_style(style):
            return style, None
        if _paragraph_style_exists(doc, "Contents 1"):
            return "Contents 1", None
        if style:
            return style, None
        return None, "Could not choose a TOC paragraph style."


class IndexesList(ToolWriterIndexBase):
    name: str | None = "indexes_list"
    intent: str | None = "navigate"
    description: str = (
        "List document indexes (TOC, alphabetical, user, bibliography tables). "
        "type matches indexes_create kind (bibliography via getServiceName). "
        "For in-flow cites use indexes_list_cites, not this tool."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}
    is_mutation: bool | None = False

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        if not hasattr(doc, "getDocumentIndexes"):
            return self._tool_error("Document does not support indexes")
        indexes = doc.getDocumentIndexes()
        count = indexes.getCount()
        result = []
        for i in range(count):
            idx = indexes.getByIndex(i)
            name = idx.getName() if hasattr(idx, "getName") else f"index_{i}"
            title = idx.Title if hasattr(idx, "Title") else ""
            result.append({
                "index": i,
                "name": name,
                "title": title,
                "type": index_kind_from_uno(idx),
            })
        return {"status": "ok", "indexes": result, "count": count}


class IndexesListCites(ToolWriterIndexBase):
    name: str | None = "indexes_list_cites"
    intent: str | None = "examine"
    description: str = (
        "List native bibliography cite fields (TextField.Bibliography). "
        "Returns identifier, key Fields (Author, Title, Year, Pages, type), and location. "
        "Does not list the bibliography table — use indexes_list for that."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}
    is_mutation: bool | None = False

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        if not hasattr(doc, "getTextFields"):
            return self._tool_error("Document does not support text fields")

        from ..search import describe_match_location

        fields = doc.getTextFields()
        enum = fields.createEnumeration()
        cites: list[Any] = []
        hf_labels: dict[int, str] = {}
        scanned = 0
        while enum.hasMoreElements():
            field = enum.nextElement()
            scanned += 1
            if not is_bibliography_text_field(field):
                continue
            mapped = {}
            try:
                mapped = fields_sequence_to_dict(field.getPropertyValue("Fields"))
            except Exception:
                mapped = {}
            try:
                location = describe_match_location(field.getAnchor(), doc, hf_labels)
            except Exception:
                location = "unknown"
            try:
                presentation = field.getPresentation(False)
            except Exception:
                presentation = ""
            cites.append({
                "id": len(cites) + 1,
                "identifier": str(mapped.get("Identifier") or ""),
                "author": mapped.get("Author", ""),
                "title": mapped.get("Title", ""),
                "year": mapped.get("Year", ""),
                "pages": mapped.get("Pages", ""),
                "bibliographic_type": mapped.get("BibiliographicType"),
                "fields": mapped,
                "location": location,
                "presentation": presentation,
            })
        return {"status": "ok", "cites": cites, "count": len(cites), "fields_scanned": scanned}


class IndexesCreate(ToolWriterIndexBase):
    name: str | None = "indexes_create"
    intent: str | None = "edit"
    description: str = (
        "Create a document index (toc, alphabetical, user, illustration, table, object, bibliography). "
        "kind=bibliography inserts the reference table (com.sun.star.text.Bibliography); "
        "cites must already exist or be added with indexes_add_mark kind=bibliography, then indexes_update_all. "
        "Use target='beginning', 'end', or 'selection'. "
        "Use target='search' with old_content to find and replace text."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["toc", "alphabetical", "user", "illustration", "table", "object", "bibliography"], "description": "The type of index to create."},
            "title": {"type": "string", "description": "The title for the index (e.g., 'Table of Contents')."},
            "create_from_outline": {"type": "boolean", "description": "Whether to create the index from the document outline (mainly for toc). Default true."},
            "target": {"type": "string", "enum": ["beginning", "end", "selection", "full_document", "search"], "description": "Where to insert the index."},
            "old_content": {"type": "string", "description": "Text to find and replace if target = 'search'."},
        },
        "required": ["kind"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        index_kind = kwargs.get("kind", "toc")
        title = kwargs.get("title")
        create_from_outline = kwargs.get("create_from_outline", True)
        target = kwargs.get("target", "selection")
        old_content = kwargs.get("old_content")

        try:
            service_map = {
                "toc": "com.sun.star.text.ContentIndex",
                "alphabetical": "com.sun.star.text.DocumentIndex",
                "user": "com.sun.star.text.UserIndex",
                "illustration": "com.sun.star.text.IllustrationsIndex",
                "table": "com.sun.star.text.TableIndex",
                "object": "com.sun.star.text.ObjectIndex",
                "bibliography": "com.sun.star.text.Bibliography",
            }
            service_name = service_map.get(index_kind, "com.sun.star.text.ContentIndex")

            index = doc.createInstance(service_name)
            if title is not None and hasattr(index, "Title"):
                index.Title = title

            if index_kind == "toc" and hasattr(index, "CreateFromOutline"):
                index.CreateFromOutline = create_from_outline

            try:
                cursor = resolve_target_cursor(ctx, target, old_content)
            except ValueError as ve:
                return self._tool_error(str(ve))

            if not cursor:
                return self._tool_error("Failed to resolve target location.")

            if target == "search" and old_content:
                cursor.setString("")

            text = cursor.getText()
            text.insertTextContent(cursor, index, False)
            index.update()

            return {"status": "ok", "message": f"Created '{index_kind}' index successfully", "title": title}
        except Exception as e:
            return self._tool_error(f"Failed to create index: {str(e)}")


class IndexesAddMark(ToolWriterIndexBase):
    name: str | None = "indexes_add_mark"
    intent: str | None = "edit"
    description: str = (
        "Insert an index mark or a bibliography cite at target. "
        "kind=alphabetical|user creates DocumentIndexMark / UserIndexMark (primary_key/secondary_key). "
        "kind=bibliography creates TextField.Bibliography — not an index mark; "
        "set Identifier/Author/Title/Year/Pages (text defaults to Identifier). "
        "primary_key is ignored for cites. After cite changes call indexes_update_all "
        "so the bibliography table refreshes. Do not use fields_insert for product cites."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Index mark entry, or Identifier fallback for kind=bibliography."},
            "kind": {
                "type": "string",
                "enum": ["alphabetical", "user", "bibliography"],
                "description": "alphabetical/user = index mark; bibliography = TextField.Bibliography cite.",
            },
            "primary_key": {"type": "string", "description": "Alphabetical index primary key. Ignored for bibliography."},
            "secondary_key": {"type": "string", "description": "Alphabetical index secondary key. Ignored for bibliography."},
            "identifier": {"type": "string", "description": "Cite key (Fields.Identifier). Defaults from text when omitted."},
            "author": {"type": "string", "description": "Cite author. Ignored unless kind=bibliography."},
            "title": {"type": "string", "description": "Cite title. Ignored unless kind=bibliography."},
            "year": {"description": "Cite year (string or integer). Ignored unless kind=bibliography."},
            "pages": {"type": "string", "description": "Cite pages / locator. Ignored unless kind=bibliography."},
            "bibliographic_type": {
                "description": "BibliographyDataType name or int (book, article, …). Ignored unless kind=bibliography.",
            },
            "fields": {
                "type": "object",
                "description": "Extra/override Fields names (Identifier, Author, ISBN, …). Ignored unless kind=bibliography.",
            },
            "target": {"type": "string", "enum": ["beginning", "end", "selection", "full_document", "search"], "description": "Where to insert the mark or cite."},
            "old_content": {"type": "string", "description": "Text to find and replace if target = 'search'."},
        },
        "required": ["text"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        unused_reserved = [key for key in _IGNORED_CITE_KWARGS if kwargs.get(key) not in (None, "")]
        doc = ctx.doc
        mark_text = kwargs.get("text")
        index_kind = kwargs.get("kind", "alphabetical")
        primary_key = kwargs.get("primary_key")
        secondary_key = kwargs.get("secondary_key")
        target = kwargs.get("target", "selection")
        old_content = kwargs.get("old_content")

        try:
            cursor = resolve_target_cursor(ctx, target, old_content)
        except ValueError as ve:
            return self._tool_error(str(ve))

        if not cursor:
            return self._tool_error("Failed to resolve target location.")

        try:
            if index_kind == "bibliography":
                return self._insert_bibliography_cite(
                    doc, cursor, kwargs, unused_reserved
                )

            service_name = "com.sun.star.text.DocumentIndexMark"
            if index_kind == "user":
                service_name = "com.sun.star.text.UserIndexMark"

            mark = doc.createInstance(service_name)

            if hasattr(mark, "MarkEntry"):
                mark.MarkEntry = mark_text
            elif hasattr(mark, "PrimaryKey") and hasattr(mark, "SecondaryKey"):
                pass  # DocumentIndexMark handles these via properties

            if index_kind == "alphabetical":
                if hasattr(mark, "PrimaryKey") and primary_key is not None:
                    mark.PrimaryKey = primary_key
                if hasattr(mark, "SecondaryKey") and secondary_key is not None:
                    mark.SecondaryKey = secondary_key
                try:
                    mark.setPropertyValue("PrimaryKey", primary_key or "")
                    mark.setPropertyValue("SecondaryKey", secondary_key or "")
                except Exception:
                    pass

            text = cursor.getText()
            text.insertTextContent(cursor, mark, False)

            return {"status": "ok", "message": f"Added '{index_kind}' index mark for '{mark_text}'"}
        except Exception as e:
            return self._tool_error(f"Failed to add index mark: {str(e)}")

    def _insert_bibliography_cite(self, doc: Any, cursor: Any, kwargs: Any, unused_reserved: Any) -> dict[str, Any]:
        pairs = collect_bibliography_field_pairs(kwargs)
        identifier = ""
        for name, value in pairs:
            if name == "Identifier":
                identifier = str(value)
                break
        if not identifier:
            return self._tool_error(
                "Bibliography cite needs identifier or text (used as Identifier)."
            )

        field = doc.createInstance(_BIB_CITE_SERVICE)
        if not field:
            return self._tool_error("Failed to create textfield.Bibliography")
        # Fields must be set on the descriptor before insert. A plain tuple is
        # dropped; set_bibliography_field_values uses a typed Any via uno.invoke.
        set_bibliography_field_values(field, pairs)
        text = cursor.getText()
        text.insertTextContent(cursor, field, False)
        payload = {
            "status": "ok",
            "message": "Added bibliography cite '%s'" % identifier,
            "kind": "bibliography",
            "identifier": identifier,
            "fields": {name: value for name, value in pairs},
        }
        if unused_reserved:
            payload["ignored"] = unused_reserved
        return payload

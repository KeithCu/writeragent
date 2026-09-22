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
changes, ``indexes_update_all`` refreshes that table. ``indexes_refresh_toc_entry``
rewrites one TOC line in place and does not call ``update()``.
"""

from typing import Any, cast

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
    name = "indexes_update_all"
    intent = "navigate"
    description = (
        "Refresh all document indexes (TOC, alphabetical, bibliography table). "
        "Call after inserting or editing bibliography cites so the reference list updates. "
        "A TOC refresh rebuilds every entry and drops customized direct formatting; "
        "use indexes_refresh_toc_entry to change one outline entry in place. "
        "Page numbers are not updated by that one-entry edit."
    )
    parameters = {"type": "object", "properties": {}, "required": []}
    is_mutation = True

    def execute(self, ctx, **kwargs):
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


def _contained(text, outer, inner) -> bool:
    """True when *inner* lies entirely inside *outer*."""
    try:
        start_ok = int(text.compareRegionStarts(outer.getStart(), inner.getStart())) >= 0
        end_ok = int(text.compareRegionEnds(inner.getEnd(), outer.getEnd())) != -1
        return start_ok and end_ok
    except Exception:
        return False


def _set_protected(idx, value: bool) -> None:
    if hasattr(idx, "setPropertyValue"):
        idx.setPropertyValue("IsProtected", value)
        return
    idx.IsProtected = value


def _entry_before(found, content: str) -> tuple[str, str]:
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


def _paragraph_string(cursor) -> str:
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


class IndexesRefreshTocEntry(ToolWriterIndexBase):
    name = "indexes_refresh_toc_entry"
    intent = "edit"
    description = (
        "Replace one substring inside a single table-of-contents entry and, when that "
        "text sits in one outline hyperlink (#…|outline), update that URL. "
        "Does not call index update(), so other entries, tabs, page numbers, and direct "
        "formatting stay. Page numbers are left as they are. "
        "indexes_update_all is the full rebuild and drops customized TOC formatting. "
        "Pass hyperlink_url to set the outline target, including when content equals "
        "old_content. Bookmark targets are not rewritten."
    )
    parameters = {
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
    is_mutation = True

    def execute(self, ctx, **kwargs) -> dict[str, Any]:
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

    def _resolve_toc(self, doc, index):
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

    def _toc_match(self, doc, anchor, old_content, occurrence):
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

    def _preview(self, found, content, override):
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

    def _write(self, ctx, doc, idx, found, content, override, preview):
        from ..edit_review import collapsed_anchor, next_agent_edit_undo_title
        from ..format import replace_preserving_format
        from ..hyperlink_fixup import (
            capture_outline_hyperlinks,
            empty_snapshot,
            restore_outline_hyperlinks,
        )

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
            return self._tool_error(
                "Cannot edit the TOC entry atomically (no usable undo context).",
                code="UNDO_UNAVAILABLE")
        applied = False
        error = None
        try:
            if was_protected:
                _set_protected(idx, False)
            try:
                snapshot = capture_outline_hyperlinks(found)
            except Exception:
                snapshot = empty_snapshot()
            if override and len(snapshot.links) != 1:
                error = self._tool_error(
                    "hyperlink_url applies only when the match overlaps exactly one outline hyperlink (|outline).",
                    code="INVALID_PARAM")
            else:
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
                applied = True
        except Exception as exc:
            error = self._tool_error(
                "TOC entry update failed; the change was rolled back (%s)." % exc,
                code="HYPERLINK_UPDATE_FAILED")
        finally:
            if was_protected:
                try:
                    _set_protected(idx, True)
                except Exception:
                    applied = False
                    if error is None:
                        error = self._tool_error(
                            "Could not restore table-of-contents protection; the edit was rolled back.")
            left = False
            try:
                mgr.leaveUndoContext()
                left = True
            except Exception:
                applied = False
                if error is None:
                    error = self._tool_error("Could not close the TOC edit undo context.")
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
            return self._tool_error("TOC entry update failed.")
        preview["status"] = "ok"
        preview["dry_run"] = False
        return preview


class IndexesList(ToolWriterIndexBase):
    name = "indexes_list"
    intent = "navigate"
    description = (
        "List document indexes (TOC, alphabetical, user, bibliography tables). "
        "type matches indexes_create kind (bibliography via getServiceName). "
        "For in-flow cites use indexes_list_cites, not this tool."
    )
    parameters = {"type": "object", "properties": {}, "required": []}
    is_mutation = False

    def execute(self, ctx, **kwargs):
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
    name = "indexes_list_cites"
    intent = "examine"
    description = (
        "List native bibliography cite fields (TextField.Bibliography). "
        "Returns identifier, key Fields (Author, Title, Year, Pages, type), and location. "
        "Does not list the bibliography table — use indexes_list for that."
    )
    parameters = {"type": "object", "properties": {}, "required": []}
    is_mutation = False

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        if not hasattr(doc, "getTextFields"):
            return self._tool_error("Document does not support text fields")

        from ..search import describe_match_location

        fields = doc.getTextFields()
        enum = fields.createEnumeration()
        cites = []
        hf_labels = {}
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
    name = "indexes_create"
    intent = "edit"
    description = (
        "Create a document index (toc, alphabetical, user, illustration, table, object, bibliography). "
        "kind=bibliography inserts the reference table (com.sun.star.text.Bibliography); "
        "cites must already exist or be added with indexes_add_mark kind=bibliography, then indexes_update_all. "
        "Use target='beginning', 'end', or 'selection'. "
        "Use target='search' with old_content to find and replace text."
    )
    parameters = {
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
    is_mutation = True

    def execute(self, ctx, **kwargs):
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
    name = "indexes_add_mark"
    intent = "edit"
    description = (
        "Insert an index mark or a bibliography cite at target. "
        "kind=alphabetical|user creates DocumentIndexMark / UserIndexMark (primary_key/secondary_key). "
        "kind=bibliography creates TextField.Bibliography — not an index mark; "
        "set Identifier/Author/Title/Year/Pages (text defaults to Identifier). "
        "primary_key is ignored for cites. After cite changes call indexes_update_all "
        "so the bibliography table refreshes. Do not use fields_insert for product cites."
    )
    parameters = {
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
    is_mutation = True

    def execute(self, ctx, **kwargs):
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

    def _insert_bibliography_cite(self, doc, cursor, kwargs, unused_reserved):
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

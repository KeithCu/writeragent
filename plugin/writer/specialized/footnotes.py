# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Specialized tools for managing Footnotes and Endnotes in Writer."""

from typing import Any

from ..specialized_base import ToolWriterFootnoteBase
from plugin.framework.errors import ToolExecutionError
from plugin.writer.search import parse_non_negative_int


def _cursor_after_nth_match(doc: Any, search: str, occurrence: int, case_sensitive: bool) -> Any:
    """Return an ``XTextCursor`` collapsed to the end of the *occurrence*-th match (0-based)."""
    sd = doc.createSearchDescriptor()
    sd.SearchString = search
    sd.SearchRegularExpression = False
    sd.SearchCaseSensitive = case_sensitive

    found = doc.findFirst(sd)
    if found is None:
        raise ToolExecutionError("No match for insert_after_text in the document.")

    for _unused in range(occurrence):
        found = doc.findNext(found, sd)
        if found is None:
            raise ToolExecutionError(f"No match for insert_after_text at occurrence {occurrence} (not enough occurrences).")

    t_cursor = found.getText().createTextCursorByRange(found)
    t_cursor.collapseToEnd()
    return t_cursor


_NOTE_TABLE: dict[str, tuple[str, str, str]] = {
    "footnote": ("getFootnotes", "getFootnoteSettings", "footnotes"),
    "endnote": ("getEndnotes", "getEndnoteSettings", "endnotes"),
}


def _note_schema(description: str) -> dict[str, Any]:
    return {"type": "string", "enum": ["footnote", "endnote"], "description": description}


def _require_note(kwargs: dict[str, Any]) -> str:
    note = kwargs.get("note")
    assert note is not None
    return str(note)


def _note_access(doc: Any, note_type: str, kind: str) -> Any:
    cfg = _NOTE_TABLE.get(note_type)
    if not cfg:
        raise ToolExecutionError(f"Invalid note_type '{note_type}'. Must be 'footnote' or 'endnote'.")
    if not doc.supportsService("com.sun.star.text.GenericTextDocument"):
        raise ToolExecutionError(f"Document does not support {cfg[2]}.")
    getter = cfg[0] if kind == "supplier" else cfg[1]
    return getattr(doc, getter)()


def _note_at_index(tool: Any, doc: Any, note_type: str, index_val: Any) -> tuple[Any, int] | dict[str, Any]:
    notes = _note_access(doc, note_type, "supplier")
    count = notes.getCount()
    # What was wrong: passing a non-numeric index to _note_at_index raised an uncaught ValueError.
    # How it happened: int(index_val) was called without catching ValueError or TypeError.
    # Why this change: catch ValueError and TypeError and return the existing invalid-index tool error.
    try:
        index = int(index_val) if index_val is not None else -1
    except (ValueError, TypeError):
        return tool._tool_error(f"Invalid index {index_val}. The document has {count} {note_type}(s).")
    if index < 0 or index >= count:
        return tool._tool_error(f"Invalid index {index}. The document has {count} {note_type}(s).")
    return notes.getByIndex(index), index


class FootnotesInsert(ToolWriterFootnoteBase):
    name: str | None = "footnotes_insert"
    description: str = (
        "Inserts a new footnote or endnote. Without insert_after, uses the current view cursor. "
        "When the insert position must be specific (e.g. delegated sub-agent work), pass insert_after: "
        "document text to find; the footnote anchor is inserted immediately after the first occurrence "
        "(or after the occurrence-th match when occurrence is set). Match the document verbatim. "
        "Note text appears at the foot of the page (footnote) or end of document (endnote). "
        "Optional custom label/mark; otherwise auto-numbered."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "note": _note_schema("Whether to insert a footnote or an endnote."),
            "text": {"type": "string", "description": "The content of the footnote or endnote."},
            "label": {"type": "string", "description": "Optional custom mark (e.g., '*'). If omitted or empty, it uses auto-numbering."},
            "insert_after": {
                "type": "string",
                "description": (
                    "If non-empty, find this substring in the document and insert the footnote/endnote "
                    "anchor immediately after that match instead of using the view cursor. "
                    "Use the exact text from the document (or from the delegating task). Required when "
                    "position matters and the tool is run outside normal user cursor control."
                ),
            },
            "occurrence": {"type": "integer", "description": ("0-based index when insert_after matches multiple times (default 0 = first match).")},
            "case_sensitive": {"type": "boolean", "description": "Search matching for insert_after (default true)."},
        },
        "required": ["note", "text"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        note_type = _require_note(kwargs)
        text = str(kwargs.get("text"))
        label = kwargs.get("label", "")

        raw_anchor = kwargs.get("insert_after")
        if raw_anchor is None:
            insert_after = ""
            use_anchor = False
        else:
            insert_after = str(raw_anchor)
            use_anchor = bool(insert_after.strip())

        doc = ctx.doc

        if use_anchor:
            # What was wrong: numeric strings, floats, and empty string were rejected.
            # How it happened: parse_non_negative_int without coerce=True rejected non-ints.
            # Why this change: restore int() coercion via coerce=True.
            occ_val, occ_err = parse_non_negative_int(kwargs.get("occurrence", 0), coerce=True)
            if occ_err:
                return self._tool_error(occ_err)
            occurrence = 0 if occ_val is None else occ_val

            case_sensitive = bool(kwargs.get("case_sensitive", True))

            try:
                t_cursor = _cursor_after_nth_match(doc, insert_after, occurrence, case_sensitive)
            except ToolExecutionError as e:
                return self._tool_error(str(e))
        else:
            # View cursor: active insertion point in the UI.
            ctrl = doc.getCurrentController()
            if not ctrl:
                return self._tool_error("Cannot access current document controller to find cursor position.")

            v_cursor = ctrl.getViewCursor()
            if not v_cursor:
                return self._tool_error("Cannot access view cursor.")

            t_cursor = v_cursor.getText().createTextCursorByRange(v_cursor)
            t_cursor.collapseToEnd()

        # Create the note instance
        service_name = "com.sun.star.text.Footnote" if note_type == "footnote" else "com.sun.star.text.Endnote"
        note = doc.createInstance(service_name)

        if label:
            note.setLabel(label)

        # Insert into document
        t_cursor.getText().insertTextContent(t_cursor, note, False)

        # Set text inside the note
        note.setString(text)

        return {"status": "ok", "message": f"Successfully inserted {note_type}.", "label": note.getLabel()}


class FootnotesList(ToolWriterFootnoteBase):
    name: str | None = "footnotes_list"
    description: str = "Lists all existing footnotes or endnotes in the document, including their indices, labels (if custom), and text content. Use this index for editing or deleting."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"note": _note_schema("Whether to list footnotes or endnotes.")}, "required": ["note"]}
    is_mutation: bool | None = False

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        note_type = _require_note(kwargs)
        doc = ctx.doc

        notes = _note_access(doc, note_type, "supplier")
        count = notes.getCount()

        results = []
        for i in range(count):
            note = notes.getByIndex(i)
            label = note.getLabel()
            text = note.getString()
            results.append({"index": i, "label": label, "text": text})

        return {"status": "ok", "count": count, "notes": results}


class FootnotesEdit(ToolWriterFootnoteBase):
    name: str | None = "footnotes_edit"
    description: str = "Edits an existing footnote or endnote. You must provide the index (from footnotes_list) and the new text content. You can optionally provide a new custom label, or set it to an empty string to revert to auto-numbering."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "note": _note_schema("Whether to edit a footnote or an endnote."),
            "index": {"type": "integer", "description": "The 0-based index of the note to edit (from footnotes_list)."},
            "text": {"type": "string", "description": "The new text content for the note."},
            "label": {"type": "string", "description": "Optional custom mark (e.g., '*'). Leave empty to revert to auto-numbering."},
        },
        "required": ["note", "index"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        note_type = _require_note(kwargs)
        res = _note_at_index(self, ctx.doc, note_type, kwargs.get("index"))
        if isinstance(res, dict):
            return res
        note, index = res

        if "text" in kwargs:
            note.setString(kwargs["text"])

        if "label" in kwargs:
            note.setLabel(kwargs["label"])

        return {"status": "ok", "message": f"Successfully edited {note_type} at index {index}."}


class FootnotesDelete(ToolWriterFootnoteBase):
    name: str | None = "footnotes_delete"
    description: str = "Deletes an existing footnote or endnote based on its index (from footnotes_list)."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {"note": _note_schema("Whether to delete a footnote or an endnote."), "index": {"type": "integer", "description": "The 0-based index of the note to delete (from footnotes_list)."}},
        "required": ["note", "index"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        note_type = _require_note(kwargs)
        res = _note_at_index(self, ctx.doc, note_type, kwargs.get("index"))
        if isinstance(res, dict):
            return res
        note, index = res

        # The note object itself is anchored somewhere.
        # We delete it by removing it from its anchor's text content.
        anchor = note.getAnchor()
        # Deleting the anchor text removes the footnote
        anchor.setString("")

        return {"status": "ok", "message": f"Successfully deleted {note_type} at index {index}."}


class FootnotesSettingsGet(ToolWriterFootnoteBase):
    name: str | None = "footnotes_settings_get"
    description: str = "Gets the current formatting and numbering settings for footnotes or endnotes. These include prefix, suffix, starting number, and styles."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"note": _note_schema("Whether to get settings for footnotes or endnotes.")}, "required": ["note"]}
    is_mutation: bool | None = False

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        note_type = _require_note(kwargs)
        doc = ctx.doc

        settings = _note_access(doc, note_type, "settings")

        # Common properties
        props = {
            "Prefix": settings.getPropertyValue("Prefix"),
            "Suffix": settings.getPropertyValue("Suffix"),
            "StartAt": settings.getPropertyValue("StartAt"),
            "NumberingType": settings.getPropertyValue("NumberingType"),
            "CharStyleName": settings.getPropertyValue("CharStyleName"),
            "AnchorCharStyleName": settings.getPropertyValue("AnchorCharStyleName"),
            "PageStyleName": settings.getPropertyValue("PageStyleName"),
            "ParaStyleName": settings.getPropertyValue("ParaStyleName"),
        }

        # Footnote-specific properties
        if note_type == "footnote":
            props["BeginNotice"] = settings.getPropertyValue("BeginNotice")
            props["EndNotice"] = settings.getPropertyValue("EndNotice")
            props["PositionEndOfDoc"] = settings.getPropertyValue("PositionEndOfDoc")
            props["FootnoteCounting"] = settings.getPropertyValue("FootnoteCounting")

        return {"status": "ok", "settings": props}


class FootnotesSettingsUpdate(ToolWriterFootnoteBase):
    name: str | None = "footnotes_settings_update"
    description: str = "Updates the formatting and numbering settings for footnotes or endnotes. You can specify which properties to change (e.g., Prefix, Suffix, StartAt, NumberingType)."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {"note": _note_schema("Whether to update settings for footnotes or endnotes."), "properties": {"type": "object", "description": "A dictionary of properties to update (e.g., {'Prefix': '[', 'Suffix': ']'})"}},
        "required": ["note", "properties"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        note_type = _require_note(kwargs)
        props_to_update = kwargs.get("properties", {})
        doc = ctx.doc

        settings = _note_access(doc, note_type, "settings")

        updated_props = []
        for prop_name, prop_val in props_to_update.items():
            try:
                # UNO sometimes requires specific types like shorts for certain properties
                # e.g., StartAt and NumberingType are shorts, but let pyuno handle the coercion mostly
                settings.setPropertyValue(prop_name, prop_val)
                updated_props.append(prop_name)
            except Exception as e:
                # What was wrong: when setPropertyValue failed after some properties were updated, the error did not report which properties had already succeeded.
                # How it happened: _tool_error only included the failure message without the updated property list.
                # Why this change: include already-updated property names in the error message and updated_properties payload.
                msg = f"Failed to set property '{prop_name}' to '{prop_val}': {str(e)}"
                if updated_props:
                    msg = f"{msg}. Already updated: {', '.join(updated_props)}."
                err = self._tool_error(msg)
                err["updated_properties"] = updated_props
                return err

        return {"status": "ok", "message": f"Successfully updated {note_type} settings.", "updated_properties": updated_props}

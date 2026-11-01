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

from plugin.writer.specialized_base import (
    ToolWriterFootnoteBase,
    enum_param,
    index_error,
)
from plugin.framework.errors import ToolExecutionError, uno_read
from plugin.writer.search import find_occurrence_range, parse_non_negative_int


NOTE_TYPES: list[str] = ["footnote", "endnote"]


def _note_guard(doc: Any, note_type: str) -> None:
    if note_type not in ("footnote", "endnote"):
        raise KeyError(note_type)
    if not doc.supportsService("com.sun.star.text.GenericTextDocument"):
        raise ToolExecutionError(f"Document does not support {note_type}s.")


def _note_supplier(doc: Any, note_type: str) -> Any:
    _note_guard(doc, note_type)
    return doc.getFootnotes() if note_type == "footnote" else doc.getEndnotes()


def _note_settings(doc: Any, note_type: str) -> Any:
    _note_guard(doc, note_type)
    return doc.getFootnoteSettings() if note_type == "footnote" else doc.getEndnoteSettings()


def _note_at_index(doc: Any, note_type: str, index: int) -> tuple[Any, Any, str | None]:
    notes = _note_supplier(doc, note_type)
    count = notes.getCount()
    err = index_error(index, count, note_type)
    if err:
        return None, None, err
    return notes, notes.getByIndex(index), None


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
            "note": enum_param(NOTE_TYPES, "Whether to insert a footnote or an endnote."),
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
        note_type = kwargs["note"]
        text = str(kwargs["text"])
        label = kwargs.get("label", "")

        insert_after = str(kwargs.get("insert_after") or "")
        doc = ctx.doc

        if insert_after.strip():
            occ_val, occ_err = parse_non_negative_int(kwargs.get("occurrence", 0), coerce=True)
            if occ_err:
                return self._tool_error(occ_err)
            occurrence = 0 if occ_val is None else occ_val

            case_sensitive = bool(kwargs.get("case_sensitive", True))

            # Shared LO search: NBSP/space normalization plus the newline-chained fallback.
            try:
                found = find_occurrence_range(
                    doc, insert_after, occurrence=occurrence, case_sensitive=case_sensitive)
            except IndexError:
                return self._tool_error(
                    f"No match for insert_after_text at occurrence {occurrence} "
                    "(not enough occurrences).")
            if found is None:
                return self._tool_error("No match for insert_after_text in the document.")
            rng = found
        else:
            # View cursor: active insertion point in the UI.
            ctrl = doc.getCurrentController()
            if not ctrl:
                return self._tool_error("Cannot access current document controller to find cursor position.")

            v_cursor = ctrl.getViewCursor()
            if not v_cursor:
                return self._tool_error("Cannot access view cursor.")

            rng = v_cursor

        t_cursor = rng.getText().createTextCursorByRange(rng)
        t_cursor.collapseToEnd()

        # Create the note instance
        service_name = "com.sun.star.text.Footnote" if note_type == "footnote" else "com.sun.star.text.Endnote"
        note = doc.createInstance(service_name)
        if not note:
            return self._tool_error(f"Failed to create a {note_type}.")

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
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {"note": enum_param(NOTE_TYPES, "Whether to list footnotes or endnotes.")},
        "required": ["note"],
    }
    is_mutation: bool | None = False

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        note_type = kwargs["note"]
        doc = ctx.doc

        notes = _note_supplier(doc, note_type)
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
            "note": enum_param(NOTE_TYPES, "Whether to edit a footnote or an endnote."),
            "index": {"type": "integer", "description": "The 0-based index of the note to edit (from footnotes_list)."},
            "text": {"type": "string", "description": "The new text content for the note."},
            "label": {"type": "string", "description": "Optional custom mark (e.g., '*'). Leave empty to revert to auto-numbering."},
        },
        "required": ["note", "index"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        if "text" not in kwargs and "label" not in kwargs:
            return self._tool_error("Provide text and/or label.")
        note_type = kwargs["note"]
        index = kwargs["index"]
        _notes, note, err = _note_at_index(ctx.doc, note_type, index)
        if err:
            return self._tool_error(err)

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
        "properties": {
            "note": enum_param(NOTE_TYPES, "Whether to delete a footnote or an endnote."),
            "index": {"type": "integer", "description": "The 0-based index of the note to delete (from footnotes_list)."},
        },
        "required": ["note", "index"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        note_type = kwargs["note"]
        index = kwargs["index"]
        notes, note, err = _note_at_index(ctx.doc, note_type, index)
        if err:
            return self._tool_error(err)
        before = notes.getCount()
        note.getAnchor().setString("")
        after = notes.getCount()
        if after < before:
            return {"status": "ok", "message": f"Successfully deleted {note_type} at index {index}."}
        # With change tracking on, anchor clearing records a tracked deletion instead of removing the note immediately.
        if uno_read(lambda: bool(ctx.doc.getPropertyValue("RecordChanges")), False):
            return {
                "status": "ok",
                "tracked": True,
                "message": (
                    f"{note_type} at index {index} is marked for deletion by tracked changes; "
                    "accept or reject it in LibreOffice (Edit > Track Changes)."
                ),
            }
        return self._tool_error(
            f"Could not delete {note_type} at index {index}: it is still in the document.")


class FootnotesSettingsGet(ToolWriterFootnoteBase):
    name: str | None = "footnotes_settings_get"
    description: str = "Gets the current formatting and numbering settings for footnotes or endnotes. These include prefix, suffix, starting number, and styles."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {"note": enum_param(NOTE_TYPES, "Whether to get settings for footnotes or endnotes.")},
        "required": ["note"],
    }
    is_mutation: bool | None = False

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        note_type = kwargs["note"]
        doc = ctx.doc

        settings = _note_settings(doc, note_type)

        names: tuple[str, ...] = (
            "Prefix",
            "Suffix",
            "StartAt",
            "NumberingType",
            "CharStyleName",
            "AnchorCharStyleName",
            "PageStyleName",
            "ParaStyleName",
        )
        if note_type == "footnote":
            names += ("BeginNotice", "EndNotice", "PositionEndOfDoc", "FootnoteCounting")
        props = {name: settings.getPropertyValue(name) for name in names}

        return {"status": "ok", "settings": props}


class FootnotesSettingsUpdate(ToolWriterFootnoteBase):
    name: str | None = "footnotes_settings_update"
    description: str = "Updates the formatting and numbering settings for footnotes or endnotes. You can specify which properties to change (e.g., Prefix, Suffix, StartAt, NumberingType)."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "note": enum_param(NOTE_TYPES, "Whether to update settings for footnotes or endnotes."),
            "properties": {"type": "object", "description": "A dictionary of properties to update (e.g., {'Prefix': '[', 'Suffix': ']'})"},
        },
        "required": ["note", "properties"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        note_type = kwargs["note"]
        props_to_update = kwargs["properties"]
        doc = ctx.doc

        settings = _note_settings(doc, note_type)

        updated_props = []
        for prop_name, prop_val in props_to_update.items():
            try:
                # UNO sometimes requires specific types like shorts for certain properties
                # e.g., StartAt and NumberingType are shorts, but let pyuno handle the coercion mostly
                settings.setPropertyValue(prop_name, prop_val)
                updated_props.append(prop_name)
            except Exception as e:
                msg = f"Failed to set property '{prop_name}' to '{prop_val}': {str(e)}"
                if updated_props:
                    msg = f"{msg}. Already updated: {', '.join(updated_props)}."
                err = self._tool_error(msg)
                err["updated_properties"] = updated_props
                return err

        return {"status": "ok", "message": f"Successfully updated {note_type} settings.", "updated_properties": updated_props}

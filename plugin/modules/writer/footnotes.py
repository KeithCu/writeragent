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

from plugin.modules.writer.base import ToolWriterFootnoteBase
from plugin.framework.errors import ToolExecutionError


def _cursor_after_nth_match(doc: Any, search: str, occurrence: int, case_sensitive: bool) -> Any:
    """Return an ``XTextCursor`` collapsed to the end of the *occurrence*-th match (0-based)."""
    sd = doc.createSearchDescriptor()
    sd.SearchString = search
    sd.SearchRegularExpression = False
    sd.SearchCaseSensitive = case_sensitive

    found = doc.findFirst(sd)
    if found is None:
        raise ToolExecutionError("No match for insert_after_text in the document.")

    for _ in range(occurrence):
        found = doc.findNext(found, sd)
        if found is None:
            raise ToolExecutionError(f"No match for insert_after_text at occurrence {occurrence} (not enough occurrences).")

    t_cursor = found.getText().createTextCursorByRange(found)
    t_cursor.collapseToEnd()
    return t_cursor


def _get_note_supplier(doc: Any, note_type: str) -> Any:
    if note_type == "footnote":
        if not doc.supportsService("com.sun.star.text.GenericTextDocument"):
            raise ToolExecutionError("Document does not support footnotes.")
        return doc.getFootnotes()
    elif note_type == "endnote":
        if not doc.supportsService("com.sun.star.text.GenericTextDocument"):
            raise ToolExecutionError("Document does not support endnotes.")
        return doc.getEndnotes()
    else:
        raise ToolExecutionError(f"Invalid note_type '{note_type}'. Must be 'footnote' or 'endnote'.")


def _get_note_settings(doc: Any, note_type: str) -> Any:
    if note_type == "footnote":
        if not doc.supportsService("com.sun.star.text.GenericTextDocument"):
            raise ToolExecutionError("Document does not support footnotes.")
        return doc.getFootnoteSettings()
    elif note_type == "endnote":
        if not doc.supportsService("com.sun.star.text.GenericTextDocument"):
            raise ToolExecutionError("Document does not support endnotes.")
        return doc.getEndnoteSettings()
    else:
        raise ToolExecutionError(f"Invalid note_type '{note_type}'. Must be 'footnote' or 'endnote'.")


class FootnotesInsert(ToolWriterFootnoteBase):
    name = "footnotes_insert"
    description = (
        "Inserts a new footnote or endnote. Without insert_after_text, uses the current view cursor. "
        "When the insert position must be specific (e.g. delegated sub-agent work), pass insert_after_text: "
        "document text to find; the footnote anchor is inserted immediately after the first occurrence "
        "(or after the occurrence-th match when occurrence is set). Match the document verbatim. "
        "Note text appears at the foot of the page (footnote) or end of document (endnote). "
        "Optional custom label/mark; otherwise auto-numbered."
    )
    parameters = {
        "type": "object",
        "properties": {
            "note_type": {"type": "string", "enum": ["footnote", "endnote"], "description": "Whether to insert a footnote or an endnote."},
            "text": {"type": "string", "description": "The content of the footnote or endnote."},
            "label": {"type": "string", "description": "Optional custom mark (e.g., '*'). If omitted or empty, it uses auto-numbering."},
            "insert_after_text": {
                "type": "string",
                "description": (
                    "If non-empty, find this substring in the document and insert the footnote/endnote "
                    "anchor immediately after that match instead of using the view cursor. "
                    "Use the exact text from the document (or from the delegating task). Required when "
                    "position matters and the tool is run outside normal user cursor control."
                ),
            },
            "occurrence": {"type": "integer", "description": ("0-based index when insert_after_text matches multiple times (default 0 = first match).")},
            "case_sensitive": {"type": "boolean", "description": "Search matching for insert_after_text (default true)."},
        },
        "required": ["note_type", "text"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        note_type = str(kwargs.get("note_type"))
        text = str(kwargs.get("text"))
        label = kwargs.get("label", "")

        raw_anchor = kwargs.get("insert_after_text")
        if raw_anchor is None:
            insert_after = ""
            use_anchor = False
        else:
            insert_after = str(raw_anchor)
            use_anchor = bool(insert_after.strip())

        doc = ctx.doc

        try:
            if use_anchor:
                occ_raw = kwargs.get("occurrence", 0)
                try:
                    occurrence = int(occ_raw)
                except (TypeError, ValueError):
                    return self._tool_error("occurrence must be an integer.")
                if occurrence < 0:
                    return self._tool_error("occurrence must be non-negative.")

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

        except Exception as e:
            return self._tool_error(f"Failed to insert {note_type}: {str(e)}")


class FootnotesList(ToolWriterFootnoteBase):
    name = "footnotes_list"
    description = "Lists all existing footnotes or endnotes in the document, including their indices, labels (if custom), and text content. Use this index for editing or deleting."
    parameters = {"type": "object", "properties": {"note_type": {"type": "string", "enum": ["footnote", "endnote"], "description": "Whether to list footnotes or endnotes."}}, "required": ["note_type"]}
    is_mutation = False

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        note_type = str(kwargs.get("note_type"))
        doc = ctx.doc

        try:
            notes = _get_note_supplier(doc, note_type)
            count = notes.getCount()

            results = []
            for i in range(count):
                note = notes.getByIndex(i)
                label = note.getLabel()
                text = note.getString()
                results.append({"index": i, "label": label, "text": text})

            return {"status": "ok", "count": count, "notes": results}
        except Exception as e:
            return self._tool_error(f"Failed to list {note_type}s: {str(e)}")


class FootnotesEdit(ToolWriterFootnoteBase):
    name = "footnotes_edit"
    description = "Edits an existing footnote or endnote. You must provide the index (from footnotes_list) and the new text content. You can optionally provide a new custom label, or set it to an empty string to revert to auto-numbering."
    parameters = {
        "type": "object",
        "properties": {
            "note_type": {"type": "string", "enum": ["footnote", "endnote"], "description": "Whether to edit a footnote or an endnote."},
            "index": {"type": "integer", "description": "The 0-based index of the note to edit (from footnotes_list)."},
            "text": {"type": "string", "description": "The new text content for the note."},
            "label": {"type": "string", "description": "Optional custom mark (e.g., '*'). Leave empty to revert to auto-numbering."},
        },
        "required": ["note_type", "index"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        note_type = str(kwargs.get("note_type"))
        index_val = kwargs.get("index")
        index = int(index_val) if index_val is not None else -1

        doc = ctx.doc

        try:
            notes = _get_note_supplier(doc, note_type)
            count = notes.getCount()

            if index < 0 or index >= count:
                return self._tool_error(f"Invalid index {index}. The document has {count} {note_type}(s).")

            note = notes.getByIndex(index)

            if "text" in kwargs:
                note.setString(kwargs["text"])

            if "label" in kwargs:
                note.setLabel(kwargs["label"])

            return {"status": "ok", "message": f"Successfully edited {note_type} at index {index}."}

        except Exception as e:
            return self._tool_error(f"Failed to edit {note_type}: {str(e)}")


class FootnotesDelete(ToolWriterFootnoteBase):
    name = "footnotes_delete"
    description = "Deletes an existing footnote or endnote based on its index (from footnotes_list)."
    parameters = {
        "type": "object",
        "properties": {"note_type": {"type": "string", "enum": ["footnote", "endnote"], "description": "Whether to delete a footnote or an endnote."}, "index": {"type": "integer", "description": "The 0-based index of the note to delete (from footnotes_list)."}},
        "required": ["note_type", "index"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        note_type = str(kwargs.get("note_type"))
        index_val = kwargs.get("index")
        index = int(index_val) if index_val is not None else -1

        doc = ctx.doc

        try:
            notes = _get_note_supplier(doc, note_type)
            count = notes.getCount()

            if index < 0 or index >= count:
                return self._tool_error(f"Invalid index {index}. The document has {count} {note_type}(s).")

            note = notes.getByIndex(index)

            # The note object itself is anchored somewhere.
            # We delete it by removing it from its anchor's text content.
            anchor = note.getAnchor()
            # Deleting the anchor text removes the footnote
            anchor.setString("")

            return {"status": "ok", "message": f"Successfully deleted {note_type} at index {index}."}

        except Exception as e:
            return self._tool_error(f"Failed to delete {note_type}: {str(e)}")


class FootnotesSettingsGet(ToolWriterFootnoteBase):
    name = "footnotes_settings_get"
    description = "Gets the current formatting and numbering settings for footnotes or endnotes. These include prefix, suffix, starting number, and styles."
    parameters = {"type": "object", "properties": {"note_type": {"type": "string", "enum": ["footnote", "endnote"], "description": "Whether to get settings for footnotes or endnotes."}}, "required": ["note_type"]}
    is_mutation = False

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        note_type = str(kwargs.get("note_type"))
        doc = ctx.doc

        try:
            settings = _get_note_settings(doc, note_type)

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
        except Exception as e:
            return self._tool_error(f"Failed to get {note_type} settings: {str(e)}")


class FootnotesSettingsUpdate(ToolWriterFootnoteBase):
    name = "footnotes_settings_update"
    description = "Updates the formatting and numbering settings for footnotes or endnotes. You can specify which properties to change (e.g., Prefix, Suffix, StartAt, NumberingType)."
    parameters = {
        "type": "object",
        "properties": {"note_type": {"type": "string", "enum": ["footnote", "endnote"], "description": "Whether to update settings for footnotes or endnotes."}, "properties": {"type": "object", "description": "A dictionary of properties to update (e.g., {'Prefix': '[', 'Suffix': ']'})"}},
        "required": ["note_type", "properties"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        note_type = str(kwargs.get("note_type"))
        props_to_update = kwargs.get("properties", {})
        doc = ctx.doc

        if not isinstance(props_to_update, dict):
            return self._tool_error("Properties must be a dictionary.")

        try:
            settings = _get_note_settings(doc, note_type)

            updated_props = []
            for prop_name, prop_val in props_to_update.items():
                try:
                    # UNO sometimes requires specific types like shorts for certain properties
                    # e.g., StartAt and NumberingType are shorts, but let pyuno handle the coercion mostly
                    settings.setPropertyValue(prop_name, prop_val)
                    updated_props.append(prop_name)
                except Exception as e:
                    return self._tool_error(f"Failed to set property '{prop_name}' to '{prop_val}': {str(e)}")

            return {"status": "ok", "message": f"Successfully updated {note_type} settings.", "updated_properties": updated_props}
        except Exception as e:
            return self._tool_error(f"Failed to update {note_type} settings: {str(e)}")

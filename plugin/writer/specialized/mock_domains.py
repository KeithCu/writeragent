# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
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
"""Mock specialized tool implementations for testing delegation across 11 new Writer domains."""

import logging


log = logging.getLogger("writeragent.writer.specialized.mock_domains")

# Mock tools: enabled when the block below is NORMAL code.
# To disable all 11 domains, wrap the block in triple quotes (add opening ''' before
# "# 1. Sections Domain" and closing ''' after CustomizationListShortcuts).

'''
# ==========================================
# 1. Sections Domain
# ==========================================

class SectionsCreate(ToolWriterSectionBase):
    name = "sections_create"
    description = "Create a new text section in the active document (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The unique name of the section to create."},
            "columns": {"type": "integer", "description": "Number of columns in the section layout.", "default": 1},
            "hide": {"type": "boolean", "description": "Whether to hide the section conditionally.", "default": False},
            "protect": {"type": "boolean", "description": "Whether to write-protect the section.", "default": False},
        },
        "required": ["name"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        name = kwargs.get("name")
        columns = kwargs.get("columns", 1)
        hide = kwargs.get("hide", False)
        protect = kwargs.get("protect", False)
        return {
            "status": "ok",
            "message": f"Mock Section '{name}' created successfully with {columns} column(s) (hide={hide}, protect={protect}).",
            "section": {"name": name, "columns": columns, "hide": hide, "protect": protect},
        }


class SectionsDelete(ToolWriterSectionBase):
    name = "sections_delete"
    description = "Delete an existing section from the document (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The name of the section to delete."},
        },
        "required": ["name"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        name = kwargs.get("name")
        return {
            "status": "ok",
            "message": f"Mock Section '{name}' deleted successfully from the document.",
        }


class SectionsList(ToolWriterSectionBase):
    name = "sections_list"
    description = "List all sections currently defined in the document (mocked)."
    parameters = {"type": "object", "properties": {}}

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        # Return a couple of mock sections for testing
        return {
            "status": "ok",
            "sections": [
                {"name": "IntroductionSection", "columns": 1, "hide": False, "protect": False},
                {"name": "AppendixSection", "columns": 2, "hide": False, "protect": True},
            ],
            "message": "Listed 2 document sections (mocked).",
        }


class SectionsSetProperties(ToolWriterSectionBase):
    name = "sections_set_properties"
    description = "Update the properties of an existing section (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The name of the section to modify."},
            "columns": {"type": "integer", "description": "Update number of columns.", "default": None},
            "is_visible": {"type": "boolean", "description": "Update visibility.", "default": None},
            "is_protected": {"type": "boolean", "description": "Update write protection.", "default": None},
        },
        "required": ["name"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        name = kwargs.get("name")
        columns = kwargs.get("columns")
        is_visible = kwargs.get("is_visible")
        is_protected = kwargs.get("is_protected")
        updates = []
        if columns is not None:
            updates.append(f"columns={columns}")
        if is_visible is not None:
            updates.append(f"is_visible={is_visible}")
        if is_protected is not None:
            updates.append(f"is_protected={is_protected}")

        updates_str = ", ".join(updates) if updates else "no changes"
        return {
            "status": "ok",
            "message": f"Mock Section '{name}' properties updated successfully: {updates_str}.",
        }


# ==========================================
# 2. Mail Merge Domain
# ==========================================

class MailMergeSetDataSource(ToolWriterMailMergeBase):
    name = "mail_merge_set_data_source"
    description = "Set the external data source (CSV/database) for the mail merge (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "source_type": {"type": "string", "description": "Type of source (e.g., CSV, Spreadsheet, Database)."},
            "path": {"type": "string", "description": "Local or absolute path to the data source file."},
        },
        "required": ["source_type", "path"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        source_type = kwargs.get("source_type")
        path = kwargs.get("path")
        return {
            "status": "ok",
            "message": f"Mock Mail Merge data source set to '{path}' ({source_type}).",
        }


class MailMergeInsertField(ToolWriterMailMergeBase):
    name = "mail_merge_insert_field"
    description = "Insert a mail merge field placeholder at a given location (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "field_name": {"type": "string", "description": "Name of the merge field (e.g., FirstName, Email)."},
            "range_locator": {"type": "string", "description": "Target placement anchor or text range locator."},
        },
        "required": ["field_name", "range_locator"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        field_name = kwargs.get("field_name")
        range_locator = kwargs.get("range_locator")
        return {
            "status": "ok",
            "message": f"Mock Mail Merge field '<{field_name}>' inserted at '{range_locator}'.",
        }


class MailMergeListFields(ToolWriterMailMergeBase):
    name = "mail_merge_list_fields"
    description = "List available fields from the configured data source (mocked)."
    parameters = {"type": "object", "properties": {}}

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "status": "ok",
            "fields": ["FirstName", "LastName", "Email", "Company", "Address", "City"],
            "message": "Listed 6 mock mail merge fields.",
        }


class MailMergeRun(ToolWriterMailMergeBase):
    name = "mail_merge_run"
    description = "Execute the mail merge operation to output files or email (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "output_type": {"type": "string", "description": "Output method: file, printer, or email."},
            "output_path": {"type": "string", "description": "Destination directory path for output files.", "default": None},
        },
        "required": ["output_type"],
    }
    is_mutation = True
    long_running = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        output_type = kwargs.get("output_type")
        output_path = kwargs.get("output_path", "/tmp/merged_docs")
        return {
            "status": "ok",
            "message": f"Mock Mail Merge executed successfully. Generated merged documents using output type '{output_type}' under '{output_path}'.",
        }


# ==========================================
# 3. Bibliography Domain
# ==========================================

class BibliographyInsertCitation(ToolWriterBibliographyBase):
    name = "bibliography_insert_citation"
    description = "Insert a bibliography citation mark into the document (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "citation_key": {"type": "string", "description": "Unique lookup key for the citation (e.g. Smith2024)."},
            "author": {"type": "string", "description": "Primary author name."},
            "title": {"type": "string", "description": "Title of the cited reference work."},
            "year": {"type": "integer", "description": "Year of publication."},
            "pages": {"type": "string", "description": "Optional page numbers or ranges cited.", "default": None},
        },
        "required": ["citation_key", "author", "title", "year"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        key = kwargs.get("citation_key")
        author = kwargs.get("author")
        title = kwargs.get("title")
        year = kwargs.get("year")
        pages = kwargs.get("pages")
        pages_str = f", pp. {pages}" if pages else ""
        return {
            "status": "ok",
            "message": f"Mock Bibliography citation '{key}' ({author}, '{title}', {year}{pages_str}) inserted successfully.",
        }


class BibliographyListCitations(ToolWriterBibliographyBase):
    name = "bibliography_list_citations"
    description = "List all citation reference keys current in the document (mocked)."
    parameters = {"type": "object", "properties": {}}

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "status": "ok",
            "citations": [
                {"citation_key": "Smith2024", "author": "Smith, J.", "title": "AI in Writing", "year": 2024},
                {"citation_key": "Doe2025", "author": "Doe, A.", "title": "Office Automation", "year": 2025},
            ],
            "message": "Listed 2 citation references (mocked).",
        }


class BibliographyGenerate(ToolWriterBibliographyBase):
    name = "bibliography_generate"
    description = "Generate/refresh the formatted bibliography index list in the document (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "style": {"type": "string", "description": "Formatting citation style (e.g., APA, MLA, IEEE, Chicago).", "default": "APA"},
        },
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        style = kwargs.get("style", "APA")
        return {
            "status": "ok",
            "message": f"Mock Bibliography index generated at the end of the document formatted using style '{style}'.",
        }


# ==========================================
# 4. Watermark Domain
# ==========================================

class WatermarkAdd(ToolWriterWatermarkBase):
    name = "watermark_add"
    description = "Add a text watermark background layer across the document pages (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Watermark message string (e.g., CONFIDENTIAL, DRAFT)."},
            "color": {"type": "string", "description": "Color name or hex code.", "default": "gray"},
            "transparency": {"type": "number", "description": "Watermark opacity value from 0 to 1.", "default": 0.5},
            "font": {"type": "string", "description": "Font family name.", "default": "Liberation Sans"},
            "angle": {"type": "integer", "description": "Diagonal angle rotation degree.", "default": 45},
        },
        "required": ["text"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        text = kwargs.get("text")
        color = kwargs.get("color", "gray")
        transparency = kwargs.get("transparency", 0.5)
        font = kwargs.get("font", "Liberation Sans")
        angle = kwargs.get("angle", 45)
        return {
            "status": "ok",
            "message": f"Mock Text Watermark '{text}' added successfully (color={color}, font={font}, transparency={transparency}, angle={angle}).",
        }


class WatermarkSetImage(ToolWriterWatermarkBase):
    name = "watermark_set_image"
    description = "Add an image watermark as a background layer (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "image_path": {"type": "string", "description": "Absolute path or URL of target watermark image file."},
            "transparency": {"type": "number", "description": "Watermark image transparency value from 0 to 1.", "default": 0.7},
        },
        "required": ["image_path"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        path = kwargs.get("image_path")
        transparency = kwargs.get("transparency", 0.7)
        return {
            "status": "ok",
            "message": f"Mock Image Watermark set to '{path}' (transparency={transparency}).",
        }


class WatermarkRemove(ToolWriterWatermarkBase):
    name = "watermark_remove"
    description = "Remove any watermark backgrounds currently in the document (mocked)."
    parameters = {"type": "object", "properties": {}}
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "status": "ok",
            "message": "Mock watermarks successfully removed from the active document.",
        }


# ==========================================
# 5. AutoText Domain
# ==========================================

class AutoTextList(ToolWriterAutoTextBase):
    name = "autotext_list"
    description = "List all registered AutoText categories and quick shortcut items (mocked)."
    parameters = {"type": "object", "properties": {}}

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "status": "ok",
            "categories": [
                {"name": "Standard", "entries": [{"shortcut": "FN", "name": "First Name"}, {"shortcut": "LN", "name": "Last Name"}]},
                {"name": "MyAutoText", "entries": [{"shortcut": "SIG", "name": "Professional Signature"}]},
            ],
            "message": "Listed 2 AutoText categories (mocked).",
        }


class AutoTextInsert(ToolWriterAutoTextBase):
    name = "autotext_insert"
    description = "Insert an AutoText entry at the current cursor location using its shortcut name (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "entry_name": {"type": "string", "description": "The AutoText shortcut abbreviation (e.g. FN, SIG)."},
        },
        "required": ["entry_name"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        entry_name = kwargs.get("entry_name")
        return {
            "status": "ok",
            "message": f"Mock AutoText entry with shortcut '{entry_name}' inserted successfully at current cursor.",
        }


class AutoTextCreateEntry(ToolWriterAutoTextBase):
    name = "autotext_create_entry"
    description = "Save the currently highlighted text as an AutoText entry (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "entry_name": {"type": "string", "description": "The unique shortcut abbreviation to assign."},
            "category": {"type": "string", "description": "Target category namespace.", "default": "MyAutoText"},
        },
        "required": ["entry_name"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        entry_name = kwargs.get("entry_name")
        category = kwargs.get("category", "MyAutoText")
        return {
            "status": "ok",
            "message": f"Mock AutoText entry '{entry_name}' created under category '{category}' from active document selection.",
        }


# ==========================================
# 6. TOC Enhancement Domain
# ==========================================

class TocEnhanceStyle(ToolWriterTocEnhancementBase):
    name = "toc_enhance_style"
    description = "Enhance the visual styling details of a Table of Contents heading tier (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "toc_level": {"type": "integer", "description": "Target TOC level (1 to 5) to restyle."},
            "font_name": {"type": "string", "description": "Assigned font family name.", "default": None},
            "font_size": {"type": "number", "description": "Font size point size.", "default": None},
            "is_bold": {"type": "boolean", "description": "Whether level text is styled as bold.", "default": None},
        },
        "required": ["toc_level"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        level = kwargs.get("toc_level")
        font = kwargs.get("font_name")
        size = kwargs.get("font_size")
        bold = kwargs.get("is_bold")
        updates = []
        if font:
            updates.append(f"font_name='{font}'")
        if size:
            updates.append(f"font_size={size}")
        if bold is not None:
            updates.append(f"is_bold={bold}")
        updates_str = ", ".join(updates) if updates else "no changes"
        return {
            "status": "ok",
            "message": f"Mock TOC Level {level} style updated successfully: {updates_str}.",
        }


class TocAddCustomEntry(ToolWriterTocEnhancementBase):
    name = "toc_add_custom_entry"
    description = "Manually insert a custom heading entry into the Table of Contents index (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "heading_text": {"type": "string", "description": "Label text displayed for the entry."},
            "target_page": {"type": "integer", "description": "Assigned page number displayed for navigation."},
            "toc_level": {"type": "integer", "description": "Indented indent structure layer tier (1 to 5).", "default": 1},
        },
        "required": ["heading_text", "target_page"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        text = kwargs.get("heading_text")
        page = kwargs.get("target_page")
        level = kwargs.get("toc_level", 1)
        return {
            "status": "ok",
            "message": f"Mock TOC custom entry '{text}' (Page {page}, Level {level}) added successfully.",
        }


class TocListEntries(ToolWriterTocEnhancementBase):
    name = "toc_list_entries"
    description = "List all manually configured custom TOC entries (mocked)."
    parameters = {"type": "object", "properties": {}}

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "status": "ok",
            "entries": [
                {"heading_text": "Executive Summary", "target_page": 2, "toc_level": 1},
                {"heading_text": "Appendix A: Mock References", "target_page": 42, "toc_level": 1},
            ],
            "message": "Listed 2 custom TOC entries (mocked).",
        }


# ==========================================
# 7. Document Automation Domain
# ==========================================

class DocAutomationRunMacro(ToolWriterDocumentAutomationBase):
    name = "doc_automation_run_macro"
    description = "Execute a Python or Basic automation macro inside the document (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "macro_name": {"type": "string", "description": "Name of target macro function to run."},
            "arguments": {"type": "array", "items": {"type": "string"}, "description": "Optional list of string arguments to pass.", "default": []},
        },
        "required": ["macro_name"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        macro = kwargs.get("macro_name")
        args = kwargs.get("arguments", [])
        return {
            "status": "ok",
            "message": f"Mock Macro '{macro}' executed successfully with arguments: {args}.",
        }


class DocAutomationBindEvent(ToolWriterDocumentAutomationBase):
    name = "doc_automation_bind_event"
    description = "Bind a macro handler trigger to a document-wide lifecycle event (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "event_name": {"type": "string", "description": "Event name trigger (e.g. OnLoad, OnSave, OnClose)."},
            "macro_name": {"type": "string", "description": "Name of macro function to call when event triggers."},
        },
        "required": ["event_name", "macro_name"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        event = kwargs.get("event_name")
        macro = kwargs.get("macro_name")
        return {
            "status": "ok",
            "message": f"Mock Document Event '{event}' bound successfully to Macro '{macro}'.",
        }


class DocAutomationListMacros(ToolWriterDocumentAutomationBase):
    name = "doc_automation_list_macros"
    description = "List all Python/Basic macros discovered in current context (mocked)."
    parameters = {"type": "object", "properties": {}}

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "status": "ok",
            "macros": [
                {"name": "user_scripts.py:auto_format_tables", "language": "Python"},
                {"name": "Standard.Module1.LogChange", "language": "Basic"},
            ],
            "message": "Listed 2 document macros (mocked).",
        }


# ==========================================
# 8. Security Domain
# ==========================================

class SecuritySignDocument(ToolWriterSecurityBase):
    name = "security_sign_document"
    description = "Attach a digital cryptographic signature stamp to protect integrity (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "certificate_id": {"type": "string", "description": "Assigned key certificate ID name for signing."},
        },
        "required": ["certificate_id"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        cert = kwargs.get("certificate_id")
        return {
            "status": "ok",
            "message": f"Mock Digital Signature applied to document using Certificate ID '{cert}'.",
        }


class SecuritySetPassword(ToolWriterSecurityBase):
    name = "security_set_password"
    description = "Set access control password passwords on the active document (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "password": {"type": "string", "description": "Security password string."},
            "permission_level": {"type": "string", "description": "Access limit tier (e.g. read_only, read_write).", "default": "read_write"},
        },
        "required": ["password"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        password = str(kwargs.get("password") or "")
        level = kwargs.get("permission_level", "read_write")
        masked_pass = "*" * len(password)
        return {
            "status": "ok",
            "message": f"Mock Password '{masked_pass}' applied with permission tier '{level}'.",
        }


class SecurityRedactText(ToolWriterSecurityBase):
    name = "security_redact_text"
    description = "Search and permanently black-out redaction pattern occurrences (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "search_pattern": {"type": "string", "description": "Search regex pattern or keyword to redact."},
            "replacement_char": {"type": "string", "description": "Replacement block character.", "default": "*"},
        },
        "required": ["search_pattern"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        pattern = kwargs.get("search_pattern")
        rep = kwargs.get("replacement_char", "*")
        return {
            "status": "ok",
            "message": f"Mock Redaction applied for pattern '{pattern}' using block character '{rep}'. Successfully scrubbed matches.",
        }


# ==========================================
# 9. Document Management Domain
# ==========================================

class DocManagementGetMetadata(ToolWriterDocumentManagementBase):
    name = "doc_management_get_metadata"
    description = "Retrieve document metadata properties and statistics (mocked)."
    parameters = {"type": "object", "properties": {}}

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "status": "ok",
            "metadata": {
                "title": "Mock Design Spec",
                "author": "KeithCu",
                "subject": "Delegation Testing Suite",
                "keywords": "writeragent, LO, specialized, mock",
                "revision_number": 42,
            },
            "message": "Retrieved mock document metadata successfully.",
        }


class DocManagementUpdateMetadata(ToolWriterDocumentManagementBase):
    name = "doc_management_update_metadata"
    description = "Update the standard or custom metadata properties in document (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "properties": {"type": "object", "description": "Dictionary of key/value properties to update."},
        },
        "required": ["properties"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        props = kwargs.get("properties", {})
        return {
            "status": "ok",
            "message": f"Mock Metadata properties updated successfully: {props}.",
        }


class DocManagementCompareWith(ToolWriterDocumentManagementBase):
    name = "doc_management_compare_with"
    description = "Compare active document content against an external file (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "other_document_path": {"type": "string", "description": "Path to the target reference document file."},
        },
        "required": ["other_document_path"],
    }

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        path = kwargs.get("other_document_path")
        return {
            "status": "ok",
            "differences": [
                {"type": "insertion", "page": 2, "line": 12, "content": "Added mock section content"},
                {"type": "deletion", "page": 4, "line": 8, "content": "Removed legacy draft paragraphs"},
            ],
            "message": f"Mock Comparison with '{path}' completed. Found 2 differences.",
        }


# ==========================================
# 10. Collaboration Domain
# ==========================================

class CollaborationListActiveUsers(ToolWriterCollaborationBase):
    name = "collaboration_list_active_users"
    description = "List other co-editors editing the shared document in real-time (mocked)."
    parameters = {"type": "object", "properties": {}}

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "status": "ok",
            "active_users": [
                {"user_id": "user_001", "name": "KeithCu", "cursor_position": "Page 1, Para 2"},
                {"user_id": "user_002", "name": "JohnBalis", "cursor_position": "Page 3, Heading 2"},
            ],
            "message": "Listed 2 active collaboration users (mocked).",
        }


class CollaborationAddNotification(ToolWriterCollaborationBase):
    name = "collaboration_add_notification"
    description = "Send a chat or edit notification message to a specific user (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "user_id": {"type": "string", "description": "The target recipient user ID."},
            "message": {"type": "string", "description": "Notification message text content."},
        },
        "required": ["user_id", "message"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        user_id = kwargs.get("user_id")
        msg = kwargs.get("message")
        return {
            "status": "ok",
            "message": f"Mock Notification message successfully dispatched to User '{user_id}': '{msg}'",
        }


class CollaborationResolveConflict(ToolWriterCollaborationBase):
    name = "collaboration_resolve_conflict"
    description = "Resolve a shared content edit sync conflict (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "conflict_id": {"type": "string", "description": "The unique reference conflict ID to resolve."},
            "resolution_strategy": {"type": "string", "description": "Strategy: mine, theirs, merge.", "default": "mine"},
        },
        "required": ["conflict_id"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        conflict_id = kwargs.get("conflict_id")
        strategy = kwargs.get("resolution_strategy", "mine")
        return {
            "status": "ok",
            "message": f"Mock Conflict '{conflict_id}' resolved successfully using strategy '{strategy}'.",
        }


# ==========================================
# 11. Customization Domain
# ==========================================

class CustomizationAddMenuItem(ToolWriterCustomizationBase):
    name = "customization_add_menu_item"
    description = "Inject a custom command action item into a Writer menu bar category (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "menu_name": {"type": "string", "description": "Name of menu category (e.g. Tools, Format, File)."},
            "item_label": {"type": "string", "description": "Display label for the new item entry."},
            "command_url": {"type": "string", "description": "Associated command trigger URL (e.g. service:my_macro)."},
        },
        "required": ["menu_name", "item_label", "command_url"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        menu = kwargs.get("menu_name")
        label = kwargs.get("item_label")
        url = kwargs.get("command_url")
        return {
            "status": "ok",
            "message": f"Mock Menu Item '{label}' successfully added to menu category '{menu}' invoking command URL '{url}'.",
        }


class CustomizationBindShortcut(ToolWriterCustomizationBase):
    name = "customization_bind_shortcut"
    description = "Assign a keyboard shortcut key to invoke a command URL (mocked)."
    parameters = {
        "type": "object",
        "properties": {
            "key_combination": {"type": "string", "description": "Key combinations (e.g. Ctrl+Shift+W, Alt+M)."},
            "command_url": {"type": "string", "description": "Target trigger URL to execute when keys are pressed."},
        },
        "required": ["key_combination", "command_url"],
    }
    is_mutation = True

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        keys = kwargs.get("key_combination")
        url = kwargs.get("command_url")
        return {
            "status": "ok",
            "message": f"Mock Key combination '{keys}' successfully bound to invoke command URL '{url}'.",
        }


class CustomizationListShortcuts(ToolWriterCustomizationBase):
    name = "customization_list_shortcuts"
    description = "List active custom keybindings and shortcuts (mocked)."
    parameters = {"type": "object", "properties": {}}

    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "status": "ok",
            "shortcuts": [
                {"key_combination": "Ctrl+Shift+G", "command_url": "service:grammar_check_all"},
                {"key_combination": "Alt+B", "command_url": "service:open_sidebar_chat"},
            ],
            "message": "Listed 2 custom keyboard shortcuts (mocked).",
        }

'''

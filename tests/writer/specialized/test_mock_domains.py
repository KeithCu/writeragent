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
"""Tests for 11 specialized Writer mock domains."""

import sys
import types
from unittest.mock import MagicMock
import pytest

# Ensure UNO is mocked if running outside LibreOffice
try:
    import uno
except ImportError:
    sys.modules["uno"] = MagicMock()
    sys.modules["unohelper"] = MagicMock()
    com_mock = MagicMock()
    sys.modules["com"] = com_mock
    # Create module type for com.sun.star.text
    css_text = types.ModuleType("com.sun.star.text")
    sys.modules["com.sun.star.text"] = css_text

from plugin.framework.tool import ToolRegistry
from plugin.writer.specialized import mock_domains

_MOCKS_ACTIVE = hasattr(mock_domains, "SectionsCreate")
pytestmark = pytest.mark.skipif(
    not _MOCKS_ACTIVE,
    reason="Mock Writer domains disabled (tool classes wrapped in ''' in mock_domains.py)",
)

if _MOCKS_ACTIVE:
    from plugin.writer.specialized.mock_domains import (
        SectionsCreate,
        SectionsDelete,
        SectionsList,
        SectionsSetProperties,
        MailMergeSetDataSource,
        MailMergeInsertField,
        MailMergeListFields,
        MailMergeRun,
        BibliographyInsertCitation,
        BibliographyListCitations,
        BibliographyGenerate,
        WatermarkAdd,
        WatermarkSetImage,
        WatermarkRemove,
        AutoTextList,
        AutoTextInsert,
        AutoTextCreateEntry,
        TocEnhanceStyle,
        TocAddCustomEntry,
        TocListEntries,
        DocAutomationRunMacro,
        DocAutomationBindEvent,
        DocAutomationListMacros,
        SecuritySignDocument,
        SecuritySetPassword,
        SecurityRedactText,
        DocManagementGetMetadata,
        DocManagementUpdateMetadata,
        DocManagementCompareWith,
        CollaborationListActiveUsers,
        CollaborationAddNotification,
        CollaborationResolveConflict,
        CustomizationAddMenuItem,
        CustomizationBindShortcut,
        CustomizationListShortcuts,
    )


@pytest.fixture
def mock_ctx():
    class DummyContext:
        def __init__(self):
            self.doc = MagicMock()
            self.doc_type = "writer"
            self.services = MagicMock()
            self.ctx = MagicMock()

    return DummyContext()


def test_mock_domains_registration():
    """Verify that all new specialized mock tools are registered in the global ToolRegistry."""
    # We mock services so auto_discover works without LibreOffice
    services = MagicMock()
    registry = ToolRegistry(services=services)
    services.tools = registry
    
    # Trigger auto-discovery of all writer tools via main writer module
    from plugin.writer import WriterModule
    writer_module = WriterModule()
    writer_module.initialize(services)
    
    # Check that our mock tools are in the registry
    expected_tools = [
        "sections_create",
        "sections_delete",
        "sections_list",
        "sections_set_properties",
        "mail_merge_set_data_source",
        "mail_merge_insert_field",
        "mail_merge_list_fields",
        "mail_merge_run",
        "bibliography_insert_citation",
        "bibliography_list_citations",
        "bibliography_generate",
        "watermark_add",
        "watermark_set_image",
        "watermark_remove",
        "autotext_list",
        "autotext_insert",
        "autotext_create_entry",
        "toc_enhance_style",
        "toc_add_custom_entry",
        "toc_list_entries",
        "doc_automation_run_macro",
        "doc_automation_bind_event",
        "doc_automation_list_macros",
        "security_sign_document",
        "security_set_password",
        "security_redact_text",
        "doc_management_get_metadata",
        "doc_management_update_metadata",
        "doc_management_compare_with",
        "collaboration_list_active_users",
        "collaboration_add_notification",
        "collaboration_resolve_conflict",
        "customization_add_menu_item",
        "customization_bind_shortcut",
        "customization_list_shortcuts",
    ]
    
    for tool_name in expected_tools:
        assert registry.get(tool_name) is not None, f"Tool '{tool_name}' not registered in registry"
        
        # Verify that they are classified under the "specialized" tier so they don't pollute the core list
        tool = registry.get(tool_name)
        assert tool.tier == "specialized", f"Tool '{tool_name}' must be of tier='specialized'"


def test_sections_tools(mock_ctx):
    # SectionsCreate
    tool_create = SectionsCreate()
    res = tool_create.execute(mock_ctx, name="MySec", columns=2, hide=True, protect=True)
    assert res["status"] == "ok"
    assert "MySec" in res["message"]
    assert res["section"]["columns"] == 2

    # SectionsDelete
    tool_delete = SectionsDelete()
    res = tool_delete.execute(mock_ctx, name="MySec")
    assert res["status"] == "ok"
    assert "deleted" in res["message"]

    # SectionsList
    tool_list = SectionsList()
    res = tool_list.execute(mock_ctx)
    assert res["status"] == "ok"
    assert len(res["sections"]) == 2

    # SectionsSetProperties
    tool_set = SectionsSetProperties()
    res = tool_set.execute(mock_ctx, name="MySec", columns=3, is_visible=False)
    assert res["status"] == "ok"
    assert "columns=3" in res["message"]


def test_mail_merge_tools(mock_ctx):
    # Set data source
    tool_source = MailMergeSetDataSource()
    res = tool_source.execute(mock_ctx, source_type="CSV", path="/home/user/data.csv")
    assert res["status"] == "ok"
    assert "data.csv" in res["message"]

    # Insert field
    tool_insert = MailMergeInsertField()
    res = tool_insert.execute(mock_ctx, field_name="FirstName", range_locator="Page 1 Para 1")
    assert res["status"] == "ok"
    assert "<FirstName>" in res["message"]

    # List fields
    tool_list = MailMergeListFields()
    res = tool_list.execute(mock_ctx)
    assert res["status"] == "ok"
    assert "FirstName" in res["fields"]

    # Run mail merge
    tool_run = MailMergeRun()
    res = tool_run.execute(mock_ctx, output_type="email", output_path="/home/user/out")
    assert res["status"] == "ok"
    assert "email" in res["message"]


def test_bibliography_tools(mock_ctx):
    # Insert citation
    tool_insert = BibliographyInsertCitation()
    res = tool_insert.execute(mock_ctx, citation_key="Knuth1984", author="Knuth, D.", title="The TeXbook", year=1984, pages="10-15")
    assert res["status"] == "ok"
    assert "Knuth1984" in res["message"]

    # List citations
    tool_list = BibliographyListCitations()
    res = tool_list.execute(mock_ctx)
    assert res["status"] == "ok"
    assert len(res["citations"]) == 2

    # Generate bibliography
    tool_gen = BibliographyGenerate()
    res = tool_gen.execute(mock_ctx, style="IEEE")
    assert res["status"] == "ok"
    assert "IEEE" in res["message"]


def test_watermark_tools(mock_ctx):
    # Add watermark
    tool_add = WatermarkAdd()
    res = tool_add.execute(mock_ctx, text="TOP SECRET", color="red", transparency=0.3)
    assert res["status"] == "ok"
    assert "TOP SECRET" in res["message"]

    # Set image watermark
    tool_img = WatermarkSetImage()
    res = tool_img.execute(mock_ctx, image_path="/path/to/logo.png")
    assert res["status"] == "ok"
    assert "logo.png" in res["message"]

    # Remove watermark
    tool_rm = WatermarkRemove()
    res = tool_rm.execute(mock_ctx)
    assert res["status"] == "ok"
    assert "removed" in res["message"]


def test_autotext_tools(mock_ctx):
    # List categories
    tool_list = AutoTextList()
    res = tool_list.execute(mock_ctx)
    assert res["status"] == "ok"
    assert len(res["categories"]) == 2

    # Insert entry
    tool_ins = AutoTextInsert()
    res = tool_ins.execute(mock_ctx, entry_name="FN")
    assert res["status"] == "ok"
    assert "FN" in res["message"]

    # Create entry
    tool_create = AutoTextCreateEntry()
    res = tool_create.execute(mock_ctx, entry_name="NEW_SIG", category="Custom")
    assert res["status"] == "ok"
    assert "NEW_SIG" in res["message"]


def test_toc_enhancement_tools(mock_ctx):
    # Enhance style
    tool_style = TocEnhanceStyle()
    res = tool_style.execute(mock_ctx, toc_level=2, font_name="Liberation Sans", font_size=12.5, is_bold=True)
    assert res["status"] == "ok"
    assert "Level 2" in res["message"]

    # Add custom entry
    tool_entry = TocAddCustomEntry()
    res = tool_entry.execute(mock_ctx, heading_text="Methodology", target_page=15, toc_level=2)
    assert res["status"] == "ok"
    assert "Methodology" in res["message"]

    # List entries
    tool_list = TocListEntries()
    res = tool_list.execute(mock_ctx)
    assert res["status"] == "ok"
    assert len(res["entries"]) == 2


def test_document_automation_tools(mock_ctx):
    # Run macro
    tool_run = DocAutomationRunMacro()
    res = tool_run.execute(mock_ctx, macro_name="format_all", arguments=["arg1", "arg2"])
    assert res["status"] == "ok"
    assert "format_all" in res["message"]

    # Bind event
    tool_bind = DocAutomationBindEvent()
    res = tool_bind.execute(mock_ctx, event_name="OnSave", macro_name="backup_db")
    assert res["status"] == "ok"
    assert "OnSave" in res["message"]

    # List macros
    tool_list = DocAutomationListMacros()
    res = tool_list.execute(mock_ctx)
    assert res["status"] == "ok"
    assert len(res["macros"]) == 2


def test_security_tools(mock_ctx):
    # Sign document
    tool_sign = SecuritySignDocument()
    res = tool_sign.execute(mock_ctx, certificate_id="cert_abc_123")
    assert res["status"] == "ok"
    assert "cert_abc_123" in res["message"]

    # Set password
    tool_pass = SecuritySetPassword()
    res = tool_pass.execute(mock_ctx, password="secretpassword", permission_level="read_only")
    assert res["status"] == "ok"
    assert "read_only" in res["message"]

    # Redact text
    tool_redact = SecurityRedactText()
    res = tool_redact.execute(mock_ctx, search_pattern=r"\b\d{3}-\d{2}-\d{4}\b")
    assert res["status"] == "ok"
    assert "redaction" in res["message"].lower()


def test_document_management_tools(mock_ctx):
    # Get metadata
    tool_get = DocManagementGetMetadata()
    res = tool_get.execute(mock_ctx)
    assert res["status"] == "ok"
    assert res["metadata"]["title"] == "Mock Design Spec"

    # Update metadata
    tool_update = DocManagementUpdateMetadata()
    res = tool_update.execute(mock_ctx, properties={"title": "New Title"})
    assert res["status"] == "ok"
    assert "New Title" in res["message"]

    # Compare document
    tool_comp = DocManagementCompareWith()
    res = tool_comp.execute(mock_ctx, other_document_path="/tmp/v1.odt")
    assert res["status"] == "ok"
    assert len(res["differences"]) == 2


def test_collaboration_tools(mock_ctx):
    # List active users
    tool_users = CollaborationListActiveUsers()
    res = tool_users.execute(mock_ctx)
    assert res["status"] == "ok"
    assert len(res["active_users"]) == 2

    # Add notification
    tool_notify = CollaborationAddNotification()
    res = tool_notify.execute(mock_ctx, user_id="user_001", message="Please review the changes.")
    assert res["status"] == "ok"
    assert "user_001" in res["message"]

    # Resolve conflict
    tool_resolve = CollaborationResolveConflict()
    res = tool_resolve.execute(mock_ctx, conflict_id="conflict_xyz", resolution_strategy="theirs")
    assert res["status"] == "ok"
    assert "conflict_xyz" in res["message"]


def test_customization_tools(mock_ctx):
    # Add menu item
    tool_menu = CustomizationAddMenuItem()
    res = tool_menu.execute(mock_ctx, menu_name="Tools", item_label="Check Grammar", command_url="service:grammar")
    assert res["status"] == "ok"
    assert "Check Grammar" in res["message"]

    # Bind shortcut
    tool_bind = CustomizationBindShortcut()
    res = tool_bind.execute(mock_ctx, key_combination="Ctrl+G", command_url="service:grammar")
    assert res["status"] == "ok"
    assert "Ctrl+G" in res["message"]

    # List shortcuts
    tool_list = CustomizationListShortcuts()
    res = tool_list.execute(mock_ctx)
    assert res["status"] == "ok"
    assert len(res["shortcuts"]) == 2

import pytest
from unittest.mock import MagicMock, patch
from plugin.doc.document_research_tools import ListOpenDocuments
from plugin.framework.tool import ToolContext, ToolRegistry


def test_list_open_documents_properties():
    tool = ListOpenDocuments()
    assert tool.name == "list_open_documents"
    assert tool.tier == "mcp"
    assert not tool.detects_mutation()


def test_list_open_documents_registry_filtering():
    services = MagicMock()
    registry = ToolRegistry(services)
    tool = ListOpenDocuments()
    registry.register(tool)

    # By default, "mcp" tier is excluded (e.g. for standard chat)
    tools = registry.get_tools()
    assert tool not in tools

    # If we customize exclude_tiers to not include "mcp", it is returned
    tools = registry.get_tools(exclude_tiers=frozenset({"specialized", "specialized_control"}))
    assert tool in tools


def test_list_open_documents_execute():
    services = MagicMock()
    context = MagicMock()
    context.ctx = MagicMock()
    context.doc = MagicMock()

    tool = ListOpenDocuments()

    mock_doc1 = MagicMock()
    mock_doc1.getURL.return_value = "file:///home/user/document.odt"
    mock_doc1.getController.return_value = None
    mock_doc1.RuntimeUID = "uid-saved-1"
    mock_doc1.isModified.return_value = True
    mock_doc2 = MagicMock()
    mock_doc2.getURL.return_value = ""  # Untitled document
    mock_doc2.getController.return_value = None
    mock_doc2.RuntimeUID = "uid-untitled-2"
    mock_doc2.isModified.return_value = False

    mock_components = [mock_doc1, mock_doc2]

    # Mock desktop.getComponents().createEnumeration()
    mock_desktop = MagicMock()
    mock_enum = MagicMock()

    # Simulate components enumeration
    elements = list(mock_components)
    mock_enum.hasMoreElements.side_effect = lambda: len(elements) > 0
    def next_elem():
        return elements.pop(0)
    mock_enum.nextElement.side_effect = next_elem
    mock_desktop.getComponents.return_value.createEnumeration.return_value = mock_enum

    with patch("plugin.framework.uno_context.get_desktop", return_value=mock_desktop), \
         patch("plugin.doc.document_research._system_path_from_url", side_effect=lambda u: u.replace("file://", "") if u else None), \
         patch("plugin.doc.document_helpers.get_document_type") as mock_get_doc_type:

        # Let mock_get_doc_type identify mock_doc2 as calc
        from plugin.doc.document_helpers import DocumentType
        mock_get_doc_type.return_value = DocumentType.CALC

        # Execute on main thread bypass patch
        with patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=lambda fn: fn()):
            result = tool.execute(context)

        assert result["status"] == "ok"
        docs = result["documents"]
        assert len(docs) == 2

        assert docs[0]["name"] == "document.odt"
        assert docs[0]["url"] == "file:///home/user/document.odt"
        assert docs[0]["doc_type"] == "writer"
        assert not docs[0]["is_active"]
        # The stable RuntimeUID is exposed so a caller can target the doc by uid.
        assert docs[0]["uid"] == "uid-saved-1"
        # The modified flag surfaces unsaved changes (the agent tells the user to save; it never
        # saves itself).
        assert docs[0]["modified"] is True

        assert docs[1]["name"] == "Untitled"
        assert docs[1]["url"] == ""
        assert docs[1]["doc_type"] == "calc"
        # An unsaved doc has no URL but still exposes a uid for targeting.
        assert docs[1]["uid"] == "uid-untitled-2"
        assert docs[1]["modified"] is False


def test_get_open_documents_lists_untitled_even_when_type_lookup_fails():
    """D5: an untitled doc (no URL) must never be dropped from the listing on a type-lookup error --
    its uid is the only handle a caller can target it by. It's listed with doc_type 'unknown'."""
    from plugin.doc.document_research import get_open_documents

    untitled = MagicMock()
    untitled.getURL.return_value = ""
    untitled.getController.return_value = None
    untitled.RuntimeUID = "uid-untitled-x"

    desktop = MagicMock()
    enum = MagicMock()
    elems = [untitled]
    enum.hasMoreElements.side_effect = lambda: len(elems) > 0
    enum.nextElement.side_effect = lambda: elems.pop(0)
    desktop.getComponents.return_value.createEnumeration.return_value = enum

    with patch("plugin.framework.uno_context.get_desktop", return_value=desktop), \
         patch("plugin.doc.document_helpers.get_document_type", side_effect=RuntimeError("boom")):
        docs = get_open_documents(MagicMock(), active_model=None)

    assert len(docs) == 1
    assert docs[0]["url"] == "" and docs[0]["uid"] == "uid-untitled-x"
    assert docs[0]["doc_type"] == "unknown"

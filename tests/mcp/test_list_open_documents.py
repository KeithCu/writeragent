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
    mock_doc2 = MagicMock()
    mock_doc2.getURL.return_value = ""  # Untitled document
    mock_doc2.getController.return_value = None

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

        assert docs[1]["name"] == "Untitled"
        assert docs[1]["url"] == ""
        assert docs[1]["doc_type"] == "calc"

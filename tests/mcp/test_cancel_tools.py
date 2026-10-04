from unittest.mock import MagicMock, patch
from plugin.framework.tool import ToolContext

@patch("plugin.framework.constants.folder_search_enabled", return_value=True)
def test_fts_cancellation(mock_search_enabled):
    from plugin.embeddings.document_research_fts_tool import SearchNearbyFiles

    tool = SearchNearbyFiles()
    stop_checker = MagicMock(return_value=True)
    ctx = ToolContext(doc=MagicMock(), ctx=MagicMock(), doc_type="writer", services=MagicMock(), stop_checker=stop_checker)

    res = tool.execute(ctx, query="test")
    assert res.get("status") == "error"
    assert res.get("message") == "Cancelled"

@patch("plugin.framework.constants.folder_search_enabled", return_value=True)
def test_search_cancellation(mock_search_enabled):
    from plugin.embeddings.document_research_search_tool import SearchEmbeddings

    tool = SearchEmbeddings()
    stop_checker = MagicMock(return_value=True)
    ctx = ToolContext(doc=MagicMock(), ctx=MagicMock(), doc_type="writer", services=MagicMock(), stop_checker=stop_checker)

    res = tool.execute(ctx, query="test")
    assert res.get("status") == "error"
    assert res.get("message") == "Cancelled"

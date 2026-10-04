from unittest.mock import MagicMock, patch
from plugin.framework.queue_executor import SendCancelled
from plugin.doc.document_research_specialized import DelegateReadDocument
from plugin.doc.document_research_grep import grep_nearby_files
from plugin.framework.tool import ToolRegistry

@patch("plugin.doc.document_research_specialized.run_inner_read_agent")
@patch("plugin.doc.document_research_specialized.close_document_research_document")
@patch("plugin.doc.document_research_specialized.open_document_for_read")
@patch("plugin.doc.document_research_specialized.resolve_path_or_name")
def test_delegate_read_document_closes_when_stopped(
    mock_resolve,
    mock_open,
    mock_close,
    mock_inner,
):
    opened_model = MagicMock()
    mock_resolve.return_value = ("/tmp/Budget.ods", "file:///tmp/Budget.ods")
    mock_open.return_value = (opened_model, "calc", None, True)

    def mock_run_on_main(fn):
        if "lambda" in fn.__name__:
            raise SendCancelled("Stopped")
        return fn()

    with patch("plugin.doc.document_research_specialized._run_on_main", side_effect=mock_run_on_main):
        tool = DelegateReadDocument()
        ctx = MagicMock()
        ctx.doc = MagicMock()
        ctx.ctx = MagicMock()
        r = ToolRegistry(services={})
        ctx.services = {"tools": r}
        ctx.stop_checker = MagicMock(return_value=True)

        tool.execute_safe(ctx, path_or_name="Budget.ods", task="Q4")

    mock_close.assert_called_once_with(opened_model, opened_for_document_research=True)


@patch("plugin.doc.document_research_grep.close_document_research_document")
@patch("plugin.doc.document_research_grep.open_document_for_read")
@patch("plugin.doc.document_research_grep.resolve_grep_candidates")
def test_grep_nearby_files_closes_when_stopped(
    mock_resolve,
    mock_open,
    mock_close,
):
    opened_model = MagicMock()
    mock_resolve.return_value = ([MagicMock(path="/tmp/Budget.ods", url="file:///tmp/Budget.ods", entry_type="file", name="Budget.ods")], False, None)
    mock_open.return_value = (opened_model, "calc", None, True)

    def mock_execute_on_main(fn, *args, **kwargs):
        if fn.__name__ == "_close":
            raise SendCancelled("Stopped")
        return fn(*args, **kwargs)

    with patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=mock_execute_on_main):
        ctx = MagicMock()
        ctx.doc = MagicMock()
        ctx.ctx = MagicMock()
        ctx.services = {}
        ctx.stop_checker = MagicMock(return_value=True)
        ctx.caller = MagicMock()

        try:
            grep_nearby_files(ctx, opened_model, {}, "pattern")
        except SendCancelled:
            pass

    mock_close.assert_called_once_with(opened_model, opened_for_document_research=True)

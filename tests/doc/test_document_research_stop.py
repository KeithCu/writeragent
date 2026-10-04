from unittest.mock import MagicMock, patch
from plugin.framework.queue_executor import SendCancelled
from plugin.doc.document_research_specialized import DelegateReadDocument
from plugin.doc.document_research_grep import grep_nearby_files
from plugin.framework.tool import ToolContext
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

    from plugin.framework.queue_executor import _current_send_cancellation

    def mock_run_on_main(fn):
        if fn.__name__ == "_do_close":
            raise SendCancelled("Stopped")
        return fn()

    with patch("plugin.doc.document_research_specialized._run_on_main", side_effect=mock_run_on_main), patch("plugin.framework.queue_executor.post_to_main_thread") as mock_post:
        # Bind a dummy scope so _current_send_cancellation.get() is not None initially
        token = _current_send_cancellation.set(MagicMock())
        try:
            tool = DelegateReadDocument()
            r = ToolRegistry(services={})
            ctx = ToolContext(
                doc=MagicMock(),
                ctx=MagicMock(),
                doc_type="calc",
                services={"tools": r},
                stop_checker=MagicMock(return_value=True),
            )

            res = tool.execute_safe(ctx, path_or_name="Budget.ods", task="Q4")
            assert res["status"] == "error"
            assert res["code"] == "USER_STOPPED"
            assert "stopped by user" in res["message"]
        finally:
            _current_send_cancellation.reset(token)

    assert mock_post.called
    assert mock_post.call_args[0][0].__name__ == '_do_close'


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

    from plugin.framework.queue_executor import _current_send_cancellation

    def mock_execute_on_main(fn, *args, **kwargs):
        if fn.__name__ == "_close":
            raise SendCancelled("Stopped")
        if fn.__name__ == "_search":
            raise SendCancelled("Stopped")
        return fn(*args, **kwargs)

    with patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=mock_execute_on_main), patch("plugin.framework.queue_executor.post_to_main_thread") as mock_post:
        token = _current_send_cancellation.set(MagicMock())
        try:
            ctx = MagicMock()
            ctx.doc = MagicMock()
            ctx.ctx = MagicMock()
            ctx.services = {}
            # Do not stop early before opening so that the finally path is reached
            ctx.stop_checker = MagicMock(return_value=False)
            ctx.caller = MagicMock()

            res = grep_nearby_files(ctx, opened_model, {}, "pattern")
            assert res == {"status": "error", "code": "USER_STOPPED", "message": "Document read stopped by user."}
        finally:
            _current_send_cancellation.reset(token)

    assert mock_post.called
    assert mock_post.call_args[0][0].__name__ == '_close'

@patch("plugin.doc.document_research_grep._grep_text_in_calc")
@patch("plugin.doc.document_research_grep.close_document_research_document")
@patch("plugin.doc.document_research_grep.open_document_for_read")
@patch("plugin.doc.document_research_grep.resolve_grep_candidates")
def test_grep_nearby_files_polls_stop_checker(
    mock_resolve,
    mock_open,
    mock_close,
    mock_grep_calc,
):
    opened_model = MagicMock()
    mock_resolve.return_value = ([MagicMock(path="/tmp/Budget.ods", url="file:///tmp/Budget.ods", entry_type="file", name="Budget.ods")], False, None)
    mock_open.return_value = (opened_model, "calc", None, True)

    def mock_execute_on_main(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=mock_execute_on_main):
        ctx = MagicMock()
        ctx.doc = MagicMock()
        ctx.ctx = MagicMock()
        ctx.services = {}
        stop_checker = MagicMock(return_value=False)
        ctx.stop_checker = stop_checker

        mock_grep_calc.return_value = ([], 0)

        grep_nearby_files(ctx, opened_model, {}, "pattern", stop_checker=stop_checker)

        mock_grep_calc.assert_called_once()
        assert mock_grep_calc.call_args.kwargs.get("stop_checker") == stop_checker

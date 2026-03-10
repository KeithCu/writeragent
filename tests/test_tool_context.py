from plugin.framework.tool_context import ToolContext

def test_tool_context_init():
    doc = object()
    ctx = object()
    doc_type = "writer"
    services = object()
    caller = "test"

    def status_cb():
        pass

    def thinking_cb():
        pass

    def stop_cb():
        return False

    tc = ToolContext(
        doc=doc,
        ctx=ctx,
        doc_type=doc_type,
        services=services,
        caller=caller,
        status_callback=status_cb,
        append_thinking_callback=thinking_cb,
        stop_checker=stop_cb
    )

    assert tc.doc is doc
    assert tc.ctx is ctx
    assert tc.doc_type == doc_type
    assert tc.services is services
    assert tc.caller == caller
    assert tc.status_callback is status_cb
    assert tc.append_thinking_callback is thinking_cb
    assert tc.stop_checker is stop_cb

def test_tool_context_defaults():
    tc = ToolContext(doc=None, ctx=None, doc_type="calc", services=None)
    assert tc.caller == ""
    assert tc.status_callback is None
    assert tc.append_thinking_callback is None
    assert tc.stop_checker is None

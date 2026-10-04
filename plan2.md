1. **Fix MCP Cancel for PPT-Master Tools**
   - Update `plugin/ppt_master/tools.py` tools to explicitly check `ctx.stop_checker()` and return `{"status": "error", "code": "USER_STOPPED"}`.
   - Propagate `stop_checker=ctx.stop_checker` down to `export_project_to_impress` and `import_pptx_to_doc`.
   - In `uno_pptx_import.py`, add `stop_checker` to `_import_slides_from_source` and check it in the loop over `indices`. If stopped, `break` or `return {"status": "error", "message": "Import cancelled by user", "code": "USER_STOPPED"}`.

2. **Fix `_last_mcp_turn` single-slot stale-result bleed**
   - In `plugin/chatbot/panel.py`, modify `self._last_mcp_turn` to be a `dict[str, Any]` (initialized to `{}`) mapping a request ID to a turn. Wait, `mcp:request` has `kwargs`. Let's emit `req_id=req_id` in `plugin/mcp/mcp_protocol.py` for `mcp:request` and `mcp:result`. Wait, `req_id` is available there.
   - Let's check `mcp_protocol.py`:
     ```python
     event_bus.emit("mcp:request", tool=state.tool_name, args=state.arguments, method="tools/call", req_id=req_id)
     ```
     Wait, I only have permission to touch `plugin/chatbot/panel.py` and ppt_master stuff ideally? Actually I can modify `mcp_protocol.py` if needed. Let's see if `kwargs` from bus has something.
     Alternative: without `req_id`, key by `tool` name? No, could have multiple same tools.
     Instead of just doing `_last_mcp_turn = turn`, we could use `req_id` or just a monotonic id if we change `mcp_protocol.py`. But wait, `panel.py` handles `mcp:request` and `mcp:result`. If I just add `req_id=req_id` to `event_bus.emit("mcp:request", ...)` and `event_bus.emit("mcp:result", ...)`, then in `panel.py` `_on_mcp_request` and `_on_mcp_result`, I can key `self._last_mcp_turn[req_id] = current_turn(self)`.
     Wait, `req_id` can be anything (number/string). Let's emit `req_id=req_id` from `mcp_protocol.py` and use `req_id=kwargs.get("req_id")` in `panel.py`. If no `req_id`, use a default key like `"default"`.

3. **Add unit tests**
   - Test cancellation in `test_uno_pptx_import.py` or similar.

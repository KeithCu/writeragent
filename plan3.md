1. **Fix MCP Cancel for PPT-Master Tools**
   - Update `plugin/ppt_master/tools.py` tools: `ExportPresentationProject`, `ValidatePptMasterProject`, `ApplyPptMasterTemplateFill`, and `ApplyPptMasterNativeEnhance` to check `if ctx.stop_checker and ctx.stop_checker(): return self._tool_error("Cancelled by user", code="USER_STOPPED")` at the very top of their `execute()` methods.
   - Plumb `stop_checker` through:
     - `plugin/ppt_master/client.py`: `export_project_to_impress`, `apply_template_fill`, `apply_native_enhance`.
     - `plugin/ppt_master/adapter/uno_pptx_deck.py`: `export_project_to_doc`.
     - `plugin/ppt_master/adapter/uno_template_fill.py`: `apply_fill_plan_file` (if it takes it). Actually wait, no need for everything to have `stop_checker` if we check it at the top, EXCEPT for long running ones like `export_project_to_doc` -> `import_pptx_to_doc`.
     - `plugin/ppt_master/adapter/uno_pptx_import.py`: `import_pptx_to_doc` and `_import_slides_from_source`. Update the loop to check `stop_checker()`.

2. **Fix `_last_mcp_turn` single-slot stale-result bleed**
   - In `plugin/chatbot/panel.py`, modify `self._last_mcp_turn` to be a dict, mapping `req_id` or just timestamp? Wait, `req_id` is available in `mcp_protocol.py` during `ParseRequestEffect` and `StreamResponseEffect`. Wait, `req_id` is `id(state)` or something. Let's see `ParseRequestEffect`. In `plugin/mcp/mcp_protocol.py`, `req_id` is passed to `ExecuteToolEffect` which receives `req_id=req_id`. Wait, in `ParseRequestEffect`, `req_id` isn't an attribute, but we can emit it.
   - Actually, wait: we can just use `req_id` from kwargs! Let's pass `req_id=req_id` in `mcp_protocol.py` to `event_bus.emit("mcp:request", ..., req_id=req_id)` and `event_bus.emit("mcp:result", ..., req_id=req_id)`.
   - Then in `plugin/chatbot/panel.py`, `self._last_mcp_turn = {}`.
   - `_on_mcp_request`: `rid = kwargs.get("req_id", ""); self._last_mcp_turn[rid] = current_turn(self)`.
   - `_on_mcp_result`: `rid = kwargs.get("req_id", ""); last_turn = self._last_mcp_turn.get(rid)`.
   - Ensure we initialize `self._last_mcp_turn = {}`.

3. **Add unit tests**
   - Test cancellation in `uno_pptx_import.py`.

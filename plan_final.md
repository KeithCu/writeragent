1. **Fix MCP Cancel for PPT-Master Tools**
   - Plumb `stop_checker: Callable[[], bool] | None = None` through:
     - `export_project_to_impress(..., stop_checker=None)`
     - `export_project_to_doc(..., stop_checker=None)`
     - `import_pptx_to_doc(..., stop_checker=None)`
     - `_import_slides_from_source(..., stop_checker=None)`
   - In `_import_slides_from_source`, add `if stop_checker and stop_checker(): return {"status": "error", "message": "Import cancelled by user", "code": "USER_STOPPED"}` inside the loop over `indices`. (Check it before `target_page = execute_on_main_thread(lambda: _ensure_target_page(bridge, out_index))`.)
   - In `plugin/ppt_master/tools.py`, check `ctx.stop_checker` at the start of `execute` for:
     - `ExportPresentationProject`
     - `ValidatePptMasterProject`
     - `ApplyPptMasterTemplateFill`
     - `ApplyPptMasterNativeEnhance`
     and if `True`, `return self._tool_error("Cancelled by user", code="USER_STOPPED")`. Also pass `stop_checker=ctx.stop_checker` to `export_project_to_impress` and similar calls if they take it. (Only `export_project_to_impress` needs it plumbed all the way for per-slide import cancel; the others are fast enough or atomic).

2. **Fix `_last_mcp_turn` single-slot stale-result bleed**
   - In `plugin/chatbot/panel.py`, modify `self._last_mcp_turn` to be `dict[Any, Any]`.
   - Update `__init__` to `self._last_mcp_turn = {}`.
   - In `plugin/mcp/mcp_protocol.py`, emit `req_id=req_id` in both `"mcp:request"` and `"mcp:result"`.
   - In `_on_mcp_request`: `rid = kwargs.get("req_id", ""); self._last_mcp_turn[rid] = current_turn(self)`.
   - In `_on_mcp_result`: `rid = kwargs.get("req_id", ""); last_turn = self._last_mcp_turn.get(rid)`. Remove the `getattr(self, "_last_mcp_turn", None)` checks and use dictionary access.

3. **Add unit tests**
   - Add/update tests in `tests/ppt_master/test_uno_pptx_import.py` and `test_tools.py` for cancel scenarios. Wait, the prompt says "Stay on PPT-Master MCP cancel / import stop." I will just add test methods.

4. **Pre Commit Steps**
   - Run tests and pre-commit checks to ensure proper testing, verifications, reviews and reflections are done.

5. **Submit PR**
   - Submit the change with descriptive commit message.

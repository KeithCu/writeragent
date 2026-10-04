1. **Fix MCP Cancel for PPT-Master Tools**
   - In `plugin/ppt_master/tools.py`, update `ExportPresentationProject`, `ValidatePptMasterProject`, `ApplyPptMasterTemplateFill`, and `ApplyPptMasterNativeEnhance` to check `ctx.stop_checker()` at the very top of their `execute()` methods and return `{"status": "error", "code": "USER_STOPPED"}` if it evaluates to `True`.
   - Plumb `ctx.stop_checker` into `export_project_to_impress` (in `client.py` and `uno_pptx_deck.py`) and down to `import_pptx_to_doc` (in `uno_pptx_import.py`).

2. **Fix `_last_mcp_turn` in Chatbot Panel**
   - In `plugin/chatbot/panel.py`, `_last_mcp_turn` currently only stores the most recent turn. This means overlapping or concurrent MCP requests could cause results to be dropped or misattributed.
   - Update `_last_mcp_turn` to be a dictionary mapping `req_id` (or similar request identifier) to the turn, or if `req_id` isn't available, map `turn.generation` or similar unique turn identifier. Or better, update it to be keyed by `req_id` if we can plumb it, but looking at `_on_mcp_request`, it might not have `req_id`. Wait, the instruction says: "key by request id / turn stamp." Let's check what `kwargs` are passed to `_on_mcp_request`. In `plugin/mcp/mcp_protocol.py`, `req_id` is available. Let's see if we can pass it. Or just key by `current_turn(self)` stamp if we have multiple in-flight? Actually, the prompt says "key by request id / turn stamp." I will use `req_id` if provided in kwargs, else `""`. Wait, `current_turn` gives a `TurnController` which has a `.generation`.

3. **Plumb cancel points in per-slide import loop**
   - In `plugin/ppt_master/adapter/uno_pptx_import.py`, modify `_import_slides_from_source` to accept `stop_checker`. Inside the slide import loop (around line 170), check `stop_checker()`. If `True`, break the loop and return `{"status": "error", "message": "Import cancelled by user", "code": "USER_STOPPED"}`. Ensure that it cleans up properly (which it should, by leaving existing slides and only deleting newly added ones if it fails, but here we intentionally abort).

4. **Add Unit Tests**
   - Add/update tests in `tests/` for `ExportPresentationProject` cancel behavior and `_import_slides_from_source` cancel.
   - Run tests.

5. **Pre-commit Checks**
   - Ensure proper testing, verification, review, and reflection are done.

6. **Submit PR**
   - Use `submit` to push changes to branch.

1. **Fix High 1: Run Python Script Tool Focus**
   - The Run Python Script tool is binding to the currently focused document, rather than the intended document.
   - Using `pin_script_document(doc)` in `plugin/scripting/python_runner.py` fixes this.
   - Added `pin_script_document(doc)` in `plugin/scripting/python_runner.py`.

2. **Fix High 2: Reset Python Session blocks UI Thread**
   - The Menubar handler for "Reset Python Session" calls `reset_workbook_python_session` which executes on the main thread and blocks it.
   - In `plugin/framework/main_shared.py`, wrapped `reset_workbook_python_session` using `run_in_background` to prevent blocking the UI thread.

3. **Fix Medium: `audio_recorder_service` uses `os.setsid`**
   - Swapped `popen_kw["preexec_fn"] = os.setsid` for `popen_kw["start_new_session"] = True` to avoid issues with preexec_fn on multithreading.

4. **Verify / Review changes**
   - Run tests ensuring the newly introduced functionalities behave expectedly.
   - Add unit tests for Isolated RPS pinning the launch doc (not focus) and for Reset not running warm/reset on the UI thread.
   - `pre_commit_instructions`

5. **Commit and create PR**
   - Submit the PR to `WIP-Fixes` branch, ensuring no forbidden words are included in the branch name, commit message or PR body.

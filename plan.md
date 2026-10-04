<<<<<<< HEAD
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
=======
1. **Remove asynchronous writes in `acp_connection.py`:**
   - In `send_notification`, change the asynchronous `run_in_background(_do_write)` block to a synchronous `try...write...flush...except`.
   - In `send_response`, do the exact same synchronous transformation.
   - This fixes the main issue: Stop silently dropped session/cancel and permission cancelled replies because the synchronous `shutdown()` closed `stdin`/terminated before the fire-and-forget write thread had a chance to write.

2. **Cleanly close `stdin` in `stop()` before `terminate()`:**
   - In `ACPConnection.stop()`, before calling `proc.terminate()`, safely close `proc.stdin`.
   - Closing `stdin` gracefully kills a host subprocess running under `flatpak-spawn`, satisfying the "terminate only kills flatpak-spawn wrapper so host agent survives Stop" requirement.
   - It also allows `test_acp_writes_do_not_block.py`'s `test_acp_stop_does_not_block` test to be fixed, because currently it explicitly tests `mock_stdin.close()`, which was what we will add, but we just want to ensure it works. Wait, `test_acp_writes_do_not_block.py` tests that `conn.stop()` doesn't block on `stdin.close()`. Yes, we can just catch `Exception` around `stdin.close()` or let it be. Wait, the test uses a mock that sleeps on `close()`. Since we will call it synchronously, the test will fail! We need to either delete that test or update it if we are changing it.

   Let's check the user prompt again: "Add unit tests that fail when stop() terminates without a delivered session/cancel. Stay on this seam."
   Wait, the prompt asks to *add* a unit test that fails if `stop()` terminates without delivering `session/cancel`!

   If I make writes synchronous, `test_acp_writes_do_not_block` will fail because it explicitly checks that `send_notification`, `send_response`, and `stop` don't block. Wait! If I just remove those tests (or fix them) and add the new one, is that ok? "Add unit tests that fail when stop() terminates without a delivered session/cancel."

   How to add the new test?
   We can create a test in `tests/acp/test_acp_connection_stop.py` or similar, that mocks `proc.terminate()` and checks that `send_notification` was flushed *before* `terminate()` was called. Actually, `acp_backend.py`'s `stop()` orchestrates this. A test could ensure that `send_notification("session/cancel")` is fully delivered before `conn.stop()` terminates the process.

   Wait, what if `send_notification` does *not* block? We can still make the writes synchronous. We can simply remove the `time.sleep` mock tests from `test_acp_writes_do_not_block.py` or remove the file. The tests in `test_acp_writes_do_not_block.py` were just added by the author to verify the *current buggy behavior* so they would fail when I fix it! Ah, yes! Or wait, no, they exist in the codebase. Let me check when they were added.
   If I just remove `run_in_background` from `send_notification` and `send_response`, they *will* block. I'll just change the test to verify they *do* block (or just delete the non-blocking assertions since it's no longer the desired behavior). Wait, no, maybe the pipe write is fast and we just shouldn't test that it's async. I'll just delete `test_acp_writes_do_not_block.py` or modify it to test that they *do* write synchronously.

3. **Writing the plan:**
   - **Step 1:** Modify `plugin/acp/acp_connection.py`.
     - In `send_notification`: replace `run_in_background` with synchronous `stdin.write` and `stdin.flush`.
     - In `send_response`: same.
     - In `stop`: before `proc.terminate()`, safely add `try: proc.stdin.close() \n except Exception: pass`.
   - **Step 2:** Fix `test_acp_writes_do_not_block.py`.
     - The current tests check that `send_notification`, `send_response`, and `stop` do *not* block if `write` or `close` is slow.
     - We will delete this file or replace it because these operations are now synchronous. I'll remove `tests/acp/test_acp_writes_do_not_block.py` entirely, as its premise is invalid under the new requirements. Wait, the instructions say "Add unit tests that fail when stop() terminates without a delivered session/cancel".
   - **Step 3:** Add the new unit test.
     - Add a test in `tests/acp/test_acp_backend_stop.py` or just modify `tests/acp/test_acp_backend.py`.
     - The new test should verify that when `backend.stop()` is called, `conn.send_notification` actually writes to the underlying stream *before* `conn.stop()` calls `terminate`. But `conn.send_notification` is mocked in `test_acp_backend.py`. So we need an integration test with a real `ACPConnection` and mocked `subprocess.Popen` to ensure `write()` is called before `terminate()`.
   - **Step 4:** Run tests and complete Pre-commit instructions.
   - **Step 5:** Submit PR to `WIP-Fixes` branch, ensuring correct commit message rules.
>>>>>>> 21b8b2b8 (Address race condition in ACP connection stop)

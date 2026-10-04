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

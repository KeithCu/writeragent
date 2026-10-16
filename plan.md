1. **Remove catch-all exception handling in `plugin/writer/tracking.py`**
   - Lines ~162-166: Remove `try...except Exception as e:` block.
   - Lines ~326-327: Remove `try...except Exception as e:` block.
   - Lines ~369-370: Remove `try...except Exception as e:` block.
   - Lines ~465-467: Remove `try...except Exception as e:` block.
   - Lines ~540-541: Remove `try...except Exception as e:` block.
   - Lines ~602-603: Remove `try...except Exception as e:` block. (Wait, let's look closer at 602. It has a `finally` block before it in lines 595-600. Keep the `finally`!)
2. **Remove catch-all exception handling in `plugin/writer/specialized/bookmarks.py`**
   - Lines ~461-462: Remove `try...except Exception as e:` block.
   - Lines ~518-519: Remove `try...except Exception as e:` block.
   - Lines ~549-550: Remove `try...except Exception as e:` block.
   - Lines ~582-583: Remove `try...except Exception as e:` block.
   - Lines ~610-611: Remove `try...except Exception as e:` block.
3. **Remove catch-all exception handling in `plugin/writer/selection.py`**
   - Lines ~69-70: Remove `try...except Exception as e:` block.
   - Lines ~91-92: Remove `try...except Exception as e:` block.
   - Lines ~111-112: Remove `try...except Exception as e:` block.
4. **Remove catch-all exception handling in `plugin/writer/specialized/fields.py`**
   - Lines ~169-170: Remove `try...except Exception as e:` block.
   - Lines ~226-227: Remove `try...except Exception as e:` block.
   - Lines ~233-234: Remove `try...except Exception as e:` block.
   - Lines ~243-244: Remove `try...except Exception as e:` block.
5. **Update tests**
   - The test updates should replace the specific `Failed to ...` message assertions with checks for `res["status"] == "error"` and check that `res["code"] == "TOOL_EXECUTION_ERROR"`. `execute_safe` returns `"Tool execution failed: ..."` so we can check for that if needed. We need to find tests testing tracking failures, bookmark failures, and update assertions to check for `Tool execution failed:` or check `res["status"] == "error"` and the correct code.
6. **Add new test**
   - Add a test in `tests/framework/test_tool_execution.py` or similar that verifies `DOCUMENT_DISPOSED` is returned if a document is disposed mid-tool execution. Wait, I should add it to a tracking test maybe? Or `tests/framework/test_tool.py`. I'll find the best place.
7. **Pre-commit and formatting**
   - I will call `pre_commit_instructions` tool and follow the instructions to ensure proper testing, verification, review, and reflection are done.

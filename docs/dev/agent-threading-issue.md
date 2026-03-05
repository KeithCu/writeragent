# Fix Web Research Tool Integration (UI Freeze) - Phase 2

## Context

In Phase 1, we successfully refactored `_start_tool_calling_async` into a flat event loop (Idea A). All callbacks are gone, and a single `while True:` loop drains a single queue `q` keeping the UI alive with `processEventsToIdle()`.

However, the tools are still executing synchronously inside the `elif kind == "stream_done":` branch. This means long-running network tools like `web_research` and `generate_image` still freeze the UI because the main thread stops draining the queue to wait for them.

**Goal of Phase 2**: Implement async tool execution within the new flat event loop architecture.

---

## Solution: Tool Queuing

Instead of executing tools in a `for tc in tool_calls:` loop inside the `"stream_done"` handler, we will:

1. Maintain a `pending_tools` list.
2. When `"stream_done"` occurs, append the tool calls to `pending_tools` and push a `"next_tool"` message onto `q`.
3. When the loop handles `"next_tool"`, it pops the first tool.
   - If it's in `ASYNC_TOOLS`, it runs it in a background thread which pushes `"tool_done"` upon completion.
   - If it's a sync tool, it runs immediately and pushes `"tool_done"`.
4. When the loop handles `"tool_done"`, it logs the result and pushes `"next_tool"` again.
5. If `"next_tool"` fires but the list is empty, we advance to the next LLM round.

This completely eliminates freezes without requiring nested drain loops!

---

## Step-by-Step Implementation

### Step 1: Add state variables before the loop

Near the top of `_start_tool_calling_async` in `plugin/chat_panel.py` (around line ~740, right set up `q` and `round_num`), add the new state:

```python
    q = queue.Queue()
    round_num = 0
    pending_tools = []
    ASYNC_TOOLS = {"web_research", "generate_image", "edit_image"}

    # Read config once for web research thinking display
    try:
        from plugin.modules.core.config import get_config, as_bool
        show_search_thinking = as_bool(get_config(self.ctx, "show_search_thinking", False))
    except Exception:
        show_search_thinking = False
```

### Step 2: Update the `"stream_done"` handler

Look for the `# --- Has tool calls: execute them synchronously ---` comment (around line ~830).
**Replace** everything from `for tc in tool_calls:` down to the `# Loop continues...` comment at the end of that block.

Replace the old synchronous loop and round-advancement with this simple handoff:
```python
                    # --- Has tool calls: queue them up ---
                    self.session.add_assistant_message(content=content, tool_calls=tool_calls)
                    if content:
                        self._append_response("\n")

                    pending_tools.extend(tool_calls)
                    q.put(("next_tool",))
```

### Step 3: Handle `"next_tool"` (Dispatch)

Add a new `elif kind == "next_tool":` branch inside the main `for item in items:` loop. This handles popping the tool, checking if it applies to the background, and kicking it off.

```python
                # --- Dispatch next tool ---
                elif kind == "next_tool":
                    if not pending_tools or self.stop_requested:
                        # --- Advance to next round ---
                        if not self.stop_requested:
                            self._set_status("Sending results to AI...")
                        round_num += 1
                        if round_num >= max_tool_rounds:
                            agent_log("chat_panel.py:exit_exhausted",
                                      "Exiting loop: exhausted max_tool_rounds",
                                      data={"rounds": max_tool_rounds}, hypothesis_id="A")
                            self._spawn_final_stream(q, client, max_tokens)
                        else:
                            self._spawn_llm_worker(q, client, max_tokens, tools, round_num)
                        continue

                    tc = pending_tools.pop(0)
                    func_name = tc.get("function", {}).get("name", "unknown")
                    func_args_str = tc.get("function", {}).get("arguments", "{}")
                    call_id = tc.get("id", "")

                    self._set_status("Running: %s" % func_name)
                    update_activity_state("tool_execute", round_num=round_num, tool_name=func_name)

                    try:
                        func_args = json.loads(func_args_str)
                    except (json.JSONDecodeError, TypeError):
                        try:
                            import ast
                            func_args = ast.literal_eval(func_args_str)
                            if not isinstance(func_args, dict):
                                func_args = {}
                        except Exception:
                            func_args = {}

                    agent_log("chat_panel.py:tool_execute", "Executing tool",
                              data={"tool": func_name, "round": round_num}, hypothesis_id="C,D,E")
                    debug_log("Tool call: %s(%s)" % (func_name, func_args_str), context="Chat")

                    image_model_override = self.image_model_selector.getText() if self.image_model_selector else None
                    if image_model_override:
                        func_args["image_model"] = image_model_override

                    def tool_status_callback(msg):
                        q.put(("status", msg))

                    if func_name in ASYNC_TOOLS:
                        # --- ASYNC EXECUTION ---
                        def run_async():
                            try:
                                def tool_thinking_callback(msg):
                                    q.put(("tool_thinking", msg))
                                
                                if supports_status:
                                    res = execute_tool_fn(func_name, func_args, model, self.ctx,
                                                          status_callback=tool_status_callback,
                                                          append_thinking_callback=tool_thinking_callback)
                                else:
                                    res = execute_tool_fn(func_name, func_args, model, self.ctx)
                                q.put(("tool_done", call_id, func_name, func_args_str, res))
                            except Exception as e:
                                q.put(("tool_done", call_id, func_name, func_args_str, json.dumps({"status": "error", "message": str(e)})))
                        
                        threading.Thread(target=run_async, daemon=True).start()
                    else:
                        # --- SYNC EXECUTION (UNO tools) ---
                        try:
                            if supports_status:
                                res = execute_tool_fn(func_name, func_args, model, self.ctx,
                                                      status_callback=tool_status_callback)
                            else:
                                res = execute_tool_fn(func_name, func_args, model, self.ctx)
                            q.put(("tool_done", call_id, func_name, func_args_str, res))
                        except Exception as e:
                            q.put(("tool_done", call_id, func_name, func_args_str, json.dumps({"status": "error", "message": str(e)})))
```

### Step 4: Handle `"tool_done"` and `"tool_thinking"`

Add these two branches to finish the implementation:

```python
                # --- Tool finished ---
                elif kind == "tool_done":
                    call_id, func_name, func_args_str, result = item[1], item[2], item[3], item[4]
                    
                    debug_log("Tool result: %s" % result, context="Chat")
                    try:
                        result_data = json.loads(result)
                        note = result_data.get("message", result_data.get("status", "done"))
                    except Exception:
                        note = "done"
                    self._append_response("[%s: %s]\n" % (func_name, note))
                    if (func_name == "apply_document_content"
                            and (note or "").strip().startswith("Replaced 0 occurrence")):
                        params_display = func_args_str if len(func_args_str) <= 800 else func_args_str[:800] + "..."
                        self._append_response("[Debug: params %s]\n" % params_display)
                    self.session.add_tool_result(call_id, result)

                    # Trigger next tool
                    q.put(("next_tool",))

                # --- Async tool thinking (e.g. web search reasoning steps) ---
                elif kind == "tool_thinking":
                    if show_search_thinking:
                        self._append_response(item[1])
```

---

## Verification Plan

### Manual Testing Steps

1. **Open** LibreOffice Writer with the LocalWriter extension sidebar.
2. **Normal chat (no tools)**: Type *"What is 2+2?"* — Verify the flat loop still handles streaming just fine.
3. **Single NO-ASYNC tool**: Type *"What does the first paragraph say?"* — Notice no freeze.
4. **ASYNC tool (the main goal!)**: Type *"Search DuckDuckGo for LibreOffice 24.2 release date"* — Notice it says "Running: web_research", and the UI allows you to click, type, and interact. It doesn't freeze the cursor! Optionally enable "Show search thinking" in Settings and verify the text appears.
5. **Multiple tools mixed**: Ask it to "Bold the first paragraph and also search the web for AAPL price". Verify the sync tool executes, hands back to the loop, the loop queues the next tool, runs the async tool in BG, UI stays alive.

If you hit any issues with typing, make sure `q.put` is importing properly. Everything should be correct in this refactor.

--- plugin/mcp/mcp_protocol.py
+++ plugin/mcp/mcp_protocol.py
@@ -809,7 +809,7 @@
                     log.debug(f"*** tools/call: {state.tool_name}, event_bus={self.event_bus} ***")
                     event_bus = getattr(self, "event_bus", None)
                     if event_bus is not None:
-                        event_bus.emit("mcp:request", tool=state.tool_name, args=state.arguments, method="tools/call")
+                        event_bus.emit("mcp:request", tool=state.tool_name, args=state.arguments, method="tools/call", req_id=req_id)

                 elif isinstance(effect, ExecuteToolEffect):
                     try:
@@ -827,7 +827,7 @@
                     event_bus = getattr(self, "event_bus", None)
                     if event_bus is not None:
                         snippet = str(effect.result)[:100] if effect.result else ""
-                        event_bus.emit("mcp:result", tool=state.tool_name, result_snippet=snippet, args=state.arguments)
+                        event_bus.emit("mcp:result", tool=state.tool_name, result_snippet=snippet, args=state.arguments, req_id=req_id)

                     # A tool may return an image: {"_mcp_image": {"data": <b64>, "mimeType": ...}} ->
                     # emit a native MCP image content block (get_image) instead of base64-as-text.

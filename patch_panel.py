--- plugin/chatbot/panel.py
+++ plugin/chatbot/panel.py
@@ -444,7 +444,7 @@
     _panel_teardown: bool
     _mcp_event_bus: Any
     _turn: Any
-    _last_mcp_turn: Any | None
+    _last_mcp_turn: dict[str, Any]

     def __init__(
         self,
@@ -509,7 +509,7 @@
         # Session I/O handles for the tool-loop interpreter (not FSM control state).
         # The queue, stripper, and document model live on ``_turn``.
         self._turn = None
-        self._last_mcp_turn = None
+        self._last_mcp_turn = {}
         self._active_client: Any = None
         self._active_max_tokens: Any = None
         self._active_tools: Any = None
@@ -1056,7 +1056,8 @@
         try:
             from plugin.chatbot.tool_loop_actions import current_turn

-            self._last_mcp_turn = current_turn(self)
+            rid = str(kwargs.get("req_id", ""))
+            self._last_mcp_turn[rid] = current_turn(self)
             from plugin.framework.logging import format_tool_call_for_display

             fmt_str = format_tool_call_for_display(tool, args, method)
@@ -1075,7 +1076,8 @@

         try:
             from plugin.chatbot.tool_loop_actions import current_turn, TurnController
-            last_turn = getattr(self, "_last_mcp_turn", None)
+            rid = str(kwargs.get("req_id", ""))
+            last_turn = self._last_mcp_turn.get(rid)
             if not isinstance(last_turn, TurnController) or current_turn(self) is not last_turn or not last_turn.alive:
                 return
         except Exception:
@@ -1086,7 +1088,8 @@
                 return
             try:
                 from plugin.chatbot.tool_loop_actions import current_turn, TurnController
-                last_turn = getattr(self, "_last_mcp_turn", None)
+                rid = str(kwargs.get("req_id", ""))
+                last_turn = self._last_mcp_turn.get(rid)
                 if not isinstance(last_turn, TurnController) or current_turn(self) is not last_turn or not last_turn.alive:
                     return
             except Exception:

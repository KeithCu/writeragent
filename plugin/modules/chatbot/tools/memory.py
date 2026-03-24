import os
import logging
from plugin.framework.tool_base import ToolBase
from plugin.framework.config import user_config_dir

log = logging.getLogger(__name__)

class MemoryStore:
    def __init__(self, ctx):
        self.config_dir = user_config_dir(ctx)
        self.memory_dir = os.path.join(self.config_dir, "memories")
        os.makedirs(self.memory_dir, exist_ok=True)
    
    def _get_path(self, target: str) -> str:
        filename = "USER.md" if target == "user" else "MEMORY.md"
        return os.path.join(self.memory_dir, filename)

    def read(self, target: str) -> str:
        path = self._get_path(target)
        if not os.path.exists(path):
            return ""
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError as e:
            log.error(f"Failed to read {target} memory: {e}")
            return ""

    def write(self, target: str, content: str) -> bool:
        path = self._get_path(target)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return True
        except OSError as e:
            log.error(f"Failed to write {target} memory: {e}")
            return False

class MemoryTool(ToolBase):
    """Persistent file-backed memory for the agent (USER profile and MEMORY notes)."""
    
    name = "memory"
    description = (
        "Persistent memory for the agent. Target 'user' stores user profile, "
        "preferences, and quirks. Target 'memory' stores project facts, "
        "environmental notes, and general agent thoughts. "
        "Actions: 'add' (appends text + newline), 'replace' (overwrites ALL content), "
        "'remove' (clears the file), 'read' (returns current content)."
    )
    uno_services = None
    tier = "core"
    intent = "navigate"
    is_mutation = False
    
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "replace", "remove", "read"],
                "description": "The action to perform."
            },
            "target": {
                "type": "string",
                "enum": ["user", "memory"],
                "description": "Which memory file to target."
            },
            "content": {
                "type": "string",
                "description": "The text to add or replace. Ignore for read/remove."
            }
        },
        "required": ["action", "target"]
    }

    def execute(self, ctx, **kwargs):
        action = kwargs.get("action")
        target = kwargs.get("target")
        content = kwargs.get("content", "")
        
        try:
            store = MemoryStore(ctx)
        except Exception as e:
            return self._tool_error(f"Failed to initialize memory store: {e}")
            
        current = store.read(target)
        
        if action == "read":
            return {"status": "ok", "target": target, "content": current}
        elif action == "remove":
            if store.write(target, ""):
                return {"status": "ok", "message": f"Cleared {target} memory."}
            return self._tool_error(f"Failed to clear {target} memory.")
        elif action == "replace":
            if store.write(target, content):
                return {"status": "ok", "message": f"Replaced {target} memory.", "new_length": len(content)}
            return self._tool_error(f"Failed to replace {target} memory.")
        elif action == "add":
            new_content = current
            if new_content and not new_content.endswith("\n"):
                new_content += "\n"
            new_content += content
            
            # Simple length limit equivalent
            if len(new_content) > 10000:
                return self._tool_error(f"Memory too large ({len(new_content)} chars). Use 'replace' to summarize.")
                
            if store.write(target, new_content):
                return {"status": "ok", "message": f"Appended to {target} memory.", "new_length": len(new_content)}
            return self._tool_error(f"Failed to append to {target} memory.")
        
        return self._tool_error(f"Unknown action: {action}")

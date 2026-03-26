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
    """Persistent file-backed memory for the agent (USER profile)."""
    
    name = "upsert_memory"
    description = (
        "Persistent memory for the agent. Stores user profile, preferences, and quirks. "
        "Inserts or updates a specific key in a YAML/JSON-like key: value structure. "
        "To delete a memory, update it with a value like 'unknown'."
    )
    uno_services = None
    tier = "core"
    intent = "navigate"
    is_mutation = False
    
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "The key to update or insert (e.g., 'favorite_color')."
            },
            "content": {
                "type": "string",
                "description": "The new value to associate with the key."
            }
        },
        "required": ["key", "content"]
    }

    def execute(self, ctx, **kwargs):
        import yaml
        key = kwargs.get("key")
        content = kwargs.get("content", "")
        
        if not key:
            return self._tool_error("Key is required.")

        try:
            store = MemoryStore(ctx)
        except Exception as e:
            return self._tool_error(f"Failed to initialize memory store: {e}")
            
        target = "user"
        current = store.read(target)
        
        try:
            parsed = yaml.safe_load(current) if current.strip() else {}
            if not isinstance(parsed, dict):
                parsed = {"_raw": current}
        except Exception:
            # Fallback if invalid yaml
            parsed = {"_raw": current}

        # Nested update
        parts = key.split(".")
        current_dict = parsed
        for part in parts[:-1]:
            if part not in current_dict or not isinstance(current_dict[part], dict):
                current_dict[part] = {}
            current_dict = current_dict[part]

        current_dict[parts[-1]] = content

        new_content = yaml.dump(parsed, sort_keys=False, allow_unicode=True)
        if store.write(target, new_content):
            return {"status": "ok", "message": f"Upserted key '{key}' in memory."}
        return self._tool_error("Failed to update memory.")

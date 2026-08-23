import os
import logging
import tempfile
import threading
from typing import Any, Mapping, cast

from plugin.framework.tool import ToolBase
from plugin.framework.config import user_config_dir
from plugin.framework.errors import ConfigError

log = logging.getLogger(__name__)

# Serializes read-modify-write cycles on the memory files. Models may emit several
# upsert_memory calls in one turn (e.g. name + favorite color); without this lock
# the concurrent writes raced: last writer clobbered sibling keys and the shorter
# payload left torn JSON tail bytes on disk (observed 2026-08-23, USER.md 150B).
_MEMORY_RW_LOCK = threading.Lock()


from plugin.framework.deal_shim import deal


class MemoryStore:
    def __init__(self, ctx):
        # crosshair: off
        self.config_dir = user_config_dir()
        if self.config_dir is None:
            raise ConfigError("UNO context is required to resolve memory store path")
        self.memory_dir = os.path.join(self.config_dir, "memories")
        os.makedirs(self.memory_dir, exist_ok=True)

    @deal.pre(lambda self, target: isinstance(target, str))
    @deal.post(lambda result: isinstance(result, str) and (result.endswith("USER.md") or result.endswith("MEMORY.md")))
    def _get_path(self, target: str) -> str:
        # crosshair: off
        filename = "USER.md" if target == "user" else "MEMORY.md"
        return os.path.join(self.memory_dir, filename)

    def read(self, target: str) -> str:
        # crosshair: off
        path = self._get_path(target)
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def write(self, target: str, content: str) -> bool:
        # crosshair: off
        path = self._get_path(target)
        # Atomic replace (same directory → same filesystem): a reader or concurrent
        # writer can never observe a half-written file, even if this process crashes
        # mid-write.
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".memory-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return True


def user_profile_exists(ctx: Any) -> bool:
    """True when USER.md has non-empty content. Missing store or I/O → False (start Librarian)."""
    try:
        store = MemoryStore(ctx)
        return bool(str(store.read("user") or "").strip())
    except Exception:
        log.debug("user_profile_exists: treating profile as missing", exc_info=True)
        return False


# Chat preview when upsert_memory runs (sidebar / librarian); value truncated for huge strings.
UPSERT_MEMORY_CHAT_VALUE_MAX = 400


@deal.post(lambda result: result is None or isinstance(result, dict))
def upsert_memory_arguments_dict(arguments: object) -> dict[str, Any] | None:
    """Normalize smolagents ToolCall.arguments (dict or JSON string) to a dict."""
    if isinstance(arguments, dict):
        return cast("dict[str, Any]", arguments)
    if isinstance(arguments, str):
        import sys

        if "crosshair" in sys.modules:
            return None
        from plugin.framework.errors import safe_json_loads

        parsed = safe_json_loads(arguments)
        return parsed if isinstance(parsed, dict) else None
    return None


@deal.post(lambda result: result is None or isinstance(result, str))
def memory_key_from_tool_arguments(arguments: object) -> str | None:
    """Extract memory key from smolagents ToolCall.arguments (dict or JSON string)."""
    d = upsert_memory_arguments_dict(arguments)
    if not d:
        return None
    k = d.get("key")
    return k if isinstance(k, str) else None


@deal.post(lambda result: isinstance(result, str) and result.endswith("\n"))
def format_upsert_memory_chat_line(func_args: Mapping[str, Any]) -> str:
    """One-line chat preview when upsert_memory starts (main chat tool loop)."""
    key = func_args.get("key")
    if not isinstance(key, str):
        return "[Running tool: upsert_memory...]\n"
    raw = func_args.get("content", "")
    if raw is None:
        val = ""
    elif isinstance(raw, str):
        val = raw
    else:
        val = str(raw)
    one_line = val.replace("\n", " ").replace("\r", " ")
    if len(one_line) > UPSERT_MEMORY_CHAT_VALUE_MAX:
        one_line = one_line[: UPSERT_MEMORY_CHAT_VALUE_MAX - 3] + "..."
    return f"[Memory update: key {key!r} value {one_line!r}]\n"


@deal.post(lambda result: isinstance(result, str) and result.endswith("\n"))
def format_upsert_memory_chat_line_from_arguments(arguments: object) -> str:
    """Chat preview for librarian ToolCall.arguments (dict or JSON string)."""
    d = upsert_memory_arguments_dict(arguments)
    if not d:
        return "[Memory update: upsert_memory]\n"
    return format_upsert_memory_chat_line(d)


class MemoryTool(ToolBase):
    """Persistent file-backed memory for the agent (USER profile)."""

    name = "upsert_memory"
    description = "Persistent memory for the agent. Stores user profile, preferences, and quirks. Inserts or updates a specific key in a YAML/JSON-like key: value structure. To delete a memory, update it with an empty string."
    uno_services = None
    tier = "core"
    intent = "navigate"
    is_mutation = False

    parameters = {"type": "object", "properties": {"key": {"type": "string", "description": "The key to update or insert (e.g., 'favorite_color')."}, "content": {"type": "string", "description": "The new value to associate with the key."}}, "required": ["key", "content"]}

    def execute(self, ctx, **kwargs):
        # crosshair: off
        import json

        key = kwargs.get("key")
        content = kwargs.get("content", "")

        if not key:
            return self._tool_error("Key is required.")

        try:
            store = MemoryStore(ctx)
        except Exception as e:
            return self._tool_error(f"Failed to initialize memory store: {e}")

        target = "user"
        # Hold the lock across the whole read-modify-write: parallel tool calls for
        # the same user message must merge, not race (torn JSON + lost keys).
        with _MEMORY_RW_LOCK:
            try:
                current = store.read(target)
            except OSError as e:
                return self._tool_error(f"Failed to read existing memory: {e}")

            try:
                parsed = json.loads(current) if current.strip() else {}
                if not isinstance(parsed, dict):
                    # Not a JSON object: start over so the librarian can rebuild memory.
                    parsed = {}
            except json.JSONDecodeError:
                # Invalid JSON (e.g. legacy YAML): start over.
                parsed = {}

            # Nested update
            parts = key.split(".")
            current_dict = parsed
            for part in parts[:-1]:
                if part not in current_dict or not isinstance(current_dict[part], dict):
                    current_dict[part] = {}
                current_dict = current_dict[part]

            current_dict[parts[-1]] = content

            # Once a real name lands, the seed marker has served its purpose.
            # Leaving it in place made the seed-guidance re-ask name/color in
            # every future session (only-once guarantee, #346).
            if key == "name":
                parsed.pop("name_source", None)

            new_content = json.dumps(parsed, indent=2, ensure_ascii=False)
            if new_content == current:
                return {"status": "ok", "message": f"Memory for '{key}' is already up to date."}

            try:
                store.write(target, new_content)
                return {"status": "ok", "message": f"Upserted key '{key}' in memory."}
            except OSError as e:
                return self._tool_error(f"Failed to write memory: {e}")

from __future__ import annotations

import json
import os
import threading
from typing import TYPE_CHECKING, Any, Mapping, cast

from plugin.framework.config import atomic_write_text, user_config_dir
from plugin.framework.constants import CHAT_DOCUMENT_CONTEXT_MAX_CHARS
from plugin.framework.deal_shim import deal
from plugin.framework.errors import ConfigError, best_effort
from plugin.framework.tool import ToolBase

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

# Librarian and chat can upsert the same USER.md from different threads.
# os.replace is atomic; the read-modify-write around it was not.
_MEMORY_WRITE_LOCK = threading.Lock()


class MemoryStore:
    path: str

    def __init__(self) -> None:
        # crosshair: off
        config_dir = user_config_dir()
        if config_dir is None:
            raise ConfigError("UNO context is required to resolve memory store path")
        self.path = os.path.join(config_dir, "memories", "USER.md")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def read(self) -> str:
        # crosshair: off
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def write(self, content: str) -> None:
        # crosshair: off
        atomic_write_text(self.path, content, prefix=".memory-", fsync=True)

    def upsert(self, key: str, content: Any) -> dict[str, Any]:
        # crosshair: off
        """Insert, update, or delete a memory entry in USER.md JSON under _MEMORY_WRITE_LOCK.

        Returns a status dict: {"status": "ok", "message": ...} or {"status": "error", "message": ...}.
        """
        with _MEMORY_WRITE_LOCK:
            try:
                current = self.read()
            except (OSError, UnicodeDecodeError) as e:
                # Invalid UTF-8 raises UnicodeDecodeError, a ValueError, not
                # an OSError. Catch both and return the same tool error as any
                # other failed read.
                return {"status": "error", "message": f"Failed to read existing memory: {e}"}

            raw = current.strip()
            if not raw:
                parsed: dict[str, Any] = {}
            else:
                try:
                    parsed_obj = json.loads(current)
                except json.JSONDecodeError:
                    # Invalid JSON used to become {} and the write replaced USER.md
                    # with only the new key. Leave the file and tell the model.
                    return {"status": "error", "message": "USER.md is not valid JSON; left unchanged."}
                if not isinstance(parsed_obj, dict):
                    return {"status": "error", "message": "USER.md is not a JSON object; left unchanged."}
                parsed = parsed_obj

            # Nested update. content "" used to store an empty string; pop the key.
            # JSON null is the same delete. A dotted key must not replace a string
            # value with {} (name="Ada" then name.nickname wiped the profile).
            parts = key.split(".")
            if content is None or content == "":
                node: Any = parsed
                for part in parts[:-1]:
                    child = node.get(part) if isinstance(node, dict) else None
                    if not isinstance(child, dict):
                        node = None
                        break
                    node = child
                if isinstance(node, dict):
                    node.pop(parts[-1], None)
            else:
                current_dict: dict[str, Any] = parsed
                for part in parts[:-1]:
                    if part in current_dict and not isinstance(current_dict.get(part), dict):
                        return {"status": "error", "message": f"Memory key '{key}' would replace a non-object value; left unchanged."}
                    child = current_dict.get(part)
                    if not isinstance(child, dict):
                        child = {}
                        current_dict[part] = child
                    current_dict = child
                current_dict[parts[-1]] = content

            try:
                new_content = "" if not parsed else json.dumps(parsed, indent=2, ensure_ascii=False)
                if len(new_content) > CHAT_DOCUMENT_CONTEXT_MAX_CHARS and len(new_content) > len(current):
                    return {
                        "status": "error",
                        "message": (
                            f"User memory would exceed maximum size limit ({len(new_content)} > "
                            f"{CHAT_DOCUMENT_CONTEXT_MAX_CHARS} characters); left unchanged."
                        ),
                    }

                if new_content == current:
                    return {"status": "ok", "message": f"Memory for '{key}' is already up to date."}

                self.write(new_content)
                return {"status": "ok", "message": f"Upserted key '{key}' in memory."}
            except (OSError, TypeError, UnicodeError) as e:
                return {"status": "error", "message": f"Failed to write memory: {e}"}


def user_memory_is_blank(val: Any) -> bool:
    """True when user memory text is empty, whitespace, or '{}'."""
    text = str(val or "").strip()
    return not text or text == "{}"


def user_profile_exists() -> bool:
    """True when USER.md has non-empty content. Missing store or I/O → False (start Librarian)."""
    val = best_effort(lambda: MemoryStore().read(), "user_profile_exists: treating profile as missing")
    return not user_memory_is_blank(val)


# Chat preview when upsert_memory runs (sidebar / librarian); value truncated for huge strings.
UPSERT_MEMORY_CHAT_VALUE_MAX = 400


def _deal_memory_args_ok(*args: Any, **kwargs: Any) -> bool:
    return True


@deal.pre(lambda arguments, *_unused, **__: _deal_memory_args_ok(arguments))
@deal.post(lambda result: result is None or isinstance(result, dict))
def upsert_memory_arguments_dict(arguments: object) -> dict[str, Any] | None:
    # crosshair: off
    """Normalize smolagents ToolCall.arguments (dict or JSON string) to a dict."""
    if isinstance(arguments, dict):
        return cast("dict[str, Any]", arguments)
    if isinstance(arguments, str):
        from plugin.chatbot.tool_loop_state import as_dict

        parsed = as_dict(arguments)
        return parsed if parsed or arguments.strip() == "{}" else None
    return None


def _deal_memory_chat_line_ok(*args: Any, **kwargs: Any) -> bool:
    return True


@deal.pre(lambda arguments: _deal_memory_chat_line_ok(arguments))
@deal.post(lambda result: isinstance(result, str) and result.endswith("\n"))
def format_upsert_memory_chat_line(arguments: object) -> str:
    """One-line chat preview when upsert_memory starts (main chat tool loop)."""
    # crosshair: off
    if not isinstance(arguments, Mapping):
        arguments = upsert_memory_arguments_dict(arguments)
        if arguments is None:
            return "[Memory update: upsert_memory]\n"

    mapping = cast("Mapping[str, Any]", arguments)
    key = mapping.get("key")
    if not isinstance(key, str):
        return "[Running tool: upsert_memory...]\n"
    raw = mapping.get("content", "")
    val = "" if raw is None else str(raw)
    one_line = val.replace("\n", " ").replace("\r", " ")
    if len(one_line) > UPSERT_MEMORY_CHAT_VALUE_MAX:
        one_line = one_line[: UPSERT_MEMORY_CHAT_VALUE_MAX - 3] + "..."
    return f"[Memory update: key {key!r} value {one_line!r}]\n"


class MemoryTool(ToolBase):
    """Persistent file-backed memory for the agent (USER profile)."""

    name: str | None = "upsert_memory"
    description: str = (
        "Persistent memory stored as a JSON object. Stores user profile, preferences, and durable facts "
        "using short stable keys. Dotted keys nest (e.g. 'writing.tone'). To delete a memory, set content to an empty string."
    )
    uno_services: list[str] | None = None
    tier: str = "core"
    is_mutation: bool | None = False

    parameters: dict[str, Any] | None = {"type": "object", "properties": {"key": {"type": "string", "description": "The key to update or insert (e.g., 'favorite_color')."}, "content": {"type": "string", "description": "The new value to associate with the key."}}, "required": ["key", "content"]}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        # crosshair: off
        key = kwargs.get("key")
        if not isinstance(key, str) or not key.strip():
            return self._tool_error("Key is required.")
        # Omit is an error. kwargs.get("content", "") treats a missing
        # argument the same as an explicit empty string, and empty content
        # is the documented delete. "" and JSON null still delete.
        if "content" not in kwargs:
            return self._tool_error("Content is required.")
        content = kwargs.get("content")

        try:
            store = MemoryStore()
        except Exception as e:
            return self._tool_error(f"Failed to initialize memory store: {e}")

        res = store.upsert(key, content)
        if res.get("status") == "error":
            return self._tool_error(res.get("message", "Failed to update memory."))
        return res

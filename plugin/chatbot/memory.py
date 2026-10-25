from __future__ import annotations

import json
import os
import threading
from typing import TYPE_CHECKING, Any, Mapping, cast

from plugin.framework.config import atomic_write_text, user_config_dir
from plugin.framework.constants import CHAT_DOCUMENT_CONTEXT_MAX_CHARS
from plugin.framework.deal_shim import (
    DEAL_MAX_CMD_ARGS,
    DEAL_MAX_SOURCE,
    DEAL_MAX_TOKEN,
    _profile,
    str_bounded,
    deal,
)
from plugin.framework.errors import ConfigError, best_effort
from plugin.framework.tool import ToolBase

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

# Librarian and chat can upsert the same USER.md from different threads.
# os.replace is atomic; the read-modify-write around it was not.
_MEMORY_WRITE_LOCK = threading.Lock()


class MemoryStore:
    memory_dir: str

    def __init__(self) -> None:
        # crosshair: off
        config_dir = user_config_dir()
        if config_dir is None:
            raise ConfigError("UNO context is required to resolve memory store path")
        self.memory_dir = os.path.join(config_dir, "memories")
        os.makedirs(self.memory_dir, exist_ok=True)

    @deal.post(lambda result: isinstance(result, str) and result.endswith("USER.md"))
    def _get_path(self) -> str:
        # crosshair: off
        return os.path.join(self.memory_dir, "USER.md")

    def read(self) -> str:
        # crosshair: off
        path = self._get_path()
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def write(self, content: str) -> None:
        # crosshair: off
        path = self._get_path()
        atomic_write_text(path, content, prefix=".memory-", fsync=True)

    def upsert(self, key: str, content: Any) -> dict[str, Any]:
        # crosshair: off
        """Insert, update, or delete a memory entry in USER.md JSON under _MEMORY_WRITE_LOCK.

        Returns a status dict: {"status": "ok", "message": ...} or {"status": "error", "message": ...}.
        """
        if not isinstance(key, str) or not key.strip():
            return {"status": "error", "message": "Key is required."}

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


def user_profile_exists(ctx: Any = None) -> bool:
    """True when USER.md has non-empty content. Missing store or I/O → False (start Librarian)."""
    val = best_effort(lambda: MemoryStore().read(), "user_profile_exists: treating profile as missing")
    text = str(val or "").strip()
    return bool(text) and text != "{}"


# Chat preview when upsert_memory runs (sidebar / librarian); value truncated for huge strings.
UPSERT_MEMORY_CHAT_VALUE_MAX = 400


def _deal_memory_args_ok_pytest(arguments: object) -> bool:
    # Pytest stays total on this LLM/ingest boundary: smolagents can hand
    # the tool a non-dict, and isinstance(arguments, (str, dict)) raises
    # PreContractError on a list. The body already returns None unless the
    # value is a dict or a JSON object string. CrossHair keeps the short
    # domain. ``arguments`` is unused.
    return True


def _deal_memory_args_ok_crosshair(arguments: object) -> bool:
    return (isinstance(arguments, str) and str_bounded(arguments, DEAL_MAX_SOURCE)) or (
        isinstance(arguments, dict)
        and len(arguments) <= DEAL_MAX_CMD_ARGS
        and (not isinstance(arguments.get("key"), str) or str_bounded(arguments.get("key"), DEAL_MAX_TOKEN))
        and (not isinstance(arguments.get("content"), str) or str_bounded(arguments.get("content"), DEAL_MAX_SOURCE))
    )


_deal_memory_args_ok = _profile(_deal_memory_args_ok_crosshair, _deal_memory_args_ok_pytest)


@deal.pre(lambda arguments, *_unused, **__: _deal_memory_args_ok(arguments))
@deal.post(lambda result: result is None or isinstance(result, dict))
def upsert_memory_arguments_dict(arguments: object) -> dict[str, Any] | None:
    # crosshair: off
    """Normalize smolagents ToolCall.arguments (dict or JSON string) to a dict."""
    if isinstance(arguments, dict):
        return cast("dict[str, Any]", arguments)
    if isinstance(arguments, str):
        from plugin.framework.errors import safe_json_loads

        parsed = safe_json_loads(arguments)
        return parsed if isinstance(parsed, dict) else None
    return None


def _deal_memory_chat_line_ok_pytest(arguments: object) -> bool:
    return True


def _deal_memory_chat_line_ok_crosshair(arguments: object) -> bool:
    if isinstance(arguments, str):
        return str_bounded(arguments, DEAL_MAX_SOURCE)
    get = getattr(arguments, "get", None)
    if not callable(get):
        return False
    key = get("key")
    content = get("content")
    return (not isinstance(key, str) or str_bounded(key, DEAL_MAX_TOKEN)) and (
        not isinstance(content, str) or str_bounded(content, DEAL_MAX_SOURCE)
    )


_deal_memory_chat_line_ok = _profile(_deal_memory_chat_line_ok_crosshair, _deal_memory_chat_line_ok_pytest)


@deal.pre(lambda arguments: _deal_memory_chat_line_ok(arguments))
@deal.post(lambda result: isinstance(result, str) and result.endswith("\n"))
def format_upsert_memory_chat_line(arguments: object) -> str:
    """One-line chat preview when upsert_memory starts (main chat tool loop)."""
    # crosshair: off
    if isinstance(arguments, Mapping):
        key = arguments.get("key")
        if not isinstance(key, str):
            return "[Running tool: upsert_memory...]\n"
        raw = arguments.get("content", "")
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

    d = upsert_memory_arguments_dict(arguments)
    if not d:
        return "[Memory update: upsert_memory]\n"
    return format_upsert_memory_chat_line(d)


class MemoryTool(ToolBase):
    """Persistent file-backed memory for the agent (USER profile)."""

    name: str | None = "upsert_memory"
    description: str = (
        "Persistent memory stored as a JSON object. Stores user profile, preferences, and durable facts "
        "using short stable keys. Dotted keys nest (e.g. 'writing.tone'). To delete a memory, set content to an empty string."
    )
    uno_services: list[str] | None = None
    tier: str = "core"
    intent: str | None = "navigate"
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

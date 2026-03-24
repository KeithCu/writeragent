"""
Experimental planning todo tool (Hermes-style) for WriterAgent.

IMPORTANT: This file is intentionally inert. All executable code is inside
this docstring so that importing this module does NOT register any tools.

To enable the tool:
1. Move the code below out of the triple-quoted string (or copy it into
   this module as real Python code).
2. Keep the class name and schema compatible with hermes-agent's todo tool
   if you want to share prompt and tool usage patterns.

Suggested implementation (commented out for now):

from plugin.framework.tool_base import ToolBase
from plugin.contrib.todo_store import TodoStore, todo_tool, VALID_STATUSES


class TodoTool(ToolBase):
    \"\"\"Session-local task list for complex multi-step work.

    This mirrors hermes-agent's `todo` tool so that planning skills and
    prompts can be reused. Use it to break large tasks into smaller steps,
    track progress, and avoid losing context across long conversations.
    \"\"\"

    name = "todo"
    description = (
        "Manage your task list for the current chat session. Use this when the "
        "user has a complex multi-step request or provides multiple tasks. "
        "Call with no parameters to read the current list. Writing: provide "
        "'todos' to create/update items; merge=false (default) replaces the "
        "entire list, merge=true updates by id and appends new items. "
        "Each item: {id: string, content: string, status: pending|in_progress|completed|cancelled}. "
        "List order is priority. Only ONE item in_progress at a time. Mark items "
        "completed immediately when done; cancel tasks that are no longer needed."
    )
    # Available for all document types
    uno_services = None
    tier = "core"
    intent = "navigate"
    is_mutation = False  # Does not touch the LibreOffice document itself

    parameters = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "Task items to write. Omit to read current list.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Unique item identifier",
                        },
                        "content": {
                            "type": "string",
                            "description": "Task description",
                        },
                        "status": {
                            "type": "string",
                            "enum": list(VALID_STATUSES),
                            "description": "Current status",
                        },
                    },
                    "required": ["id", "content", "status"],
                },
            },
            "merge": {
                "type": "boolean",
                "description": (
                    "true: update existing items by id and add new ones. "
                    "false (default): replace the entire list."
                ),
                "default": False,
            },
        },
        "required": [],
    }

    def execute(self, ctx, **kwargs):
        # Expect a session-scoped TodoStore to be provided via ctx.services.
        store = None
        services = getattr(ctx, "services", None) or {}
        if isinstance(services, dict):
            store = services.get("todo_store")
        if store is None:
            # Fallback: create a local store so the tool still returns something,
            # but this will not persist across calls. In practice, callers should
            # inject a shared TodoStore per chat session.
            store = TodoStore()

        todos = kwargs.get("todos")
        merge = bool(kwargs.get("merge", False))
        result_json = todo_tool(todos=todos, merge=merge, store=store)
        try:
            data = json.loads(result_json)
        except (json.JSONDecodeError, TypeError):
            return {"status": "error", "message": "Invalid JSON from todo_tool"}
        return {"status": "ok", **data}

"""


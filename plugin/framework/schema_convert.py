"""Convert between OpenAI function-calling and MCP tool schemas."""

import copy


def to_openai_schema(tool):
    """Convert a ToolBase instance to an OpenAI function-calling schema.

    Returns::

        {
            "type": "function",
            "function": {
                "name": "get_document_tree",
                "description": "...",
                "parameters": { ... JSON Schema ... }
            }
        }
    """
    params = copy.deepcopy(tool.parameters) if tool.parameters else {}
    if "type" not in params:
        params["type"] = "object"

    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": params,
        },
    }


def to_mcp_schema(tool):
    """Convert a ToolBase instance to an MCP tools/list schema.

    Returns::

        {
            "name": "get_document_outline",
            "description": "...",
            "inputSchema": { ... JSON Schema ... }
        }
    """
    input_schema = copy.deepcopy(tool.parameters) if tool.parameters else {}
    if "type" not in input_schema:
        input_schema["type"] = "object"

    return {
        "name": tool.name,
        "description": tool.description or "",
        "inputSchema": input_schema,
    }

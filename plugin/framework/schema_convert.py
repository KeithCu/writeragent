# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Convert between OpenAI function-calling and MCP tool schemas."""

import copy


def _normalize_schema_for_strict_providers(params):
    """Normalize JSON Schema so strict providers (e.g. Gemini via OpenRouter) accept it.

    - Union types (e.g. \"type\": [\"string\", \"array\"]) are replaced with the first type.
    - Empty \"required\" is removed so providers do not complain about required[0/1] missing.
    - Nested properties are normalized recursively.
    """
    if not params or not isinstance(params, dict):
        return params
    params = copy.deepcopy(params)
    if "type" in params and isinstance(params["type"], list):
        types = params["type"]
        params["type"] = "array" if "array" in types else (types[0] if types else "string")
    if params.get("type") != "array":
        params.pop("items", None)
    if params.get("required") == []:
        params.pop("required", None)
    for key in ("properties", "items"):
        if key in params and isinstance(params[key], dict):
            if key == "properties":
                for k, v in params[key].items():
                    params[key][k] = _normalize_schema_for_strict_providers(v)
            else:
                params[key] = _normalize_schema_for_strict_providers(params[key])
        elif key in params and isinstance(params[key], list):
            # items can be a list of schemas in JSON Schema; take first
            if params[key]:
                params[key] = _normalize_schema_for_strict_providers(params[key][0])
    return params


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
    params = _normalize_schema_for_strict_providers(params)

    return {"type": "function", "function": {"name": tool.name, "description": tool.description or "", "parameters": params}}


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

    return {"name": tool.name, "description": tool.description or "", "inputSchema": input_schema}

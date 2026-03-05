"""Auto-generated module manifest. DO NOT EDIT."""

VERSION = '1.7.3'

MODULES = [
    {
        "name": "main",
        "title": "LocalWriter global settings",
        "requires": [],
        "provides_services": [],
        "config": {},
        "actions": [
                "about"
        ],
        "action_icons": {}
},
    {
        "name": "core",
        "title": "Core services (config, events, logging)",
        "requires": [],
        "provides_services": [
                "document",
                "config",
                "events",
                "format"
        ],
        "config": {},
        "actions": [],
        "action_icons": {}
},
    {
        "name": "http",
        "title": "HTTP / MCP Services",
        "requires": [
                "config",
                "events"
        ],
        "provides_services": [
                "http_routes"
        ],
        "config": {
                "mcp_enabled": {
                        "type": "boolean",
                        "default": False,
                        "widget": "checkbox",
                        "label": "Enable MCP Server",
                        "helper": "Localhost only, no auth. Access via stdio is always enabled.",
                        "public": True
                },
                "mcp_port": {
                        "type": "int",
                        "default": 8765,
                        "min": 1024,
                        "max": 65535,
                        "widget": "number",
                        "label": "MCP Port",
                        "public": True
                }
        },
        "actions": [],
        "action_icons": {}
},
    {
        "name": "ai",
        "title": "AI provider registry and model catalog",
        "requires": [
                "config",
                "events"
        ],
        "provides_services": [
                "ai"
        ],
        "config": {},
        "actions": [],
        "action_icons": {}
},
    {
        "name": "draw",
        "title": "Draw and Impress tools",
        "requires": [
                "document",
                "config",
                "ai"
        ],
        "provides_services": [],
        "config": {},
        "actions": [],
        "action_icons": {}
},
    {
        "name": "calc",
        "title": "Calc spreadsheet tools",
        "requires": [
                "document",
                "config"
        ],
        "provides_services": [],
        "config": {
                "max_rows_display": {
                        "type": "int",
                        "default": 1000,
                        "min": 100,
                        "max": 100000,
                        "widget": "number",
                        "label": "Max Rows Display",
                        "public": True
                }
        },
        "actions": [],
        "action_icons": {}
},
    {
        "name": "batch",
        "title": "Batch tool execution with variable chaining",
        "requires": [
                "document",
                "config",
                "events"
        ],
        "provides_services": [],
        "config": {},
        "actions": [],
        "action_icons": {}
},
    {
        "name": "writer",
        "title": "Writer document tools (including navigation and search)",
        "requires": [
                "document",
                "config",
                "format",
                "ai",
                "events"
        ],
        "provides_services": [
                "writer_bookmarks",
                "writer_tree",
                "writer_proximity",
                "writer_index"
        ],
        "config": {
                "max_content_chars": {
                        "type": "int",
                        "default": 50000,
                        "min": 1000,
                        "max": 500000,
                        "widget": "number",
                        "label": "Max Content Size",
                        "public": True
                }
        },
        "actions": [],
        "action_icons": {}
},
    {
        "name": "chatbot",
        "title": "AI chat sidebar and REST API",
        "requires": [
                "document",
                "config",
                "events",
                "ai",
                "http_routes"
        ],
        "provides_services": [],
        "config": {
                "max_tool_rounds": {
                        "type": "int",
                        "default": 15,
                        "min": 1,
                        "max": 50,
                        "widget": "number",
                        "label": "Max Tool Rounds"
                },
                "context_strategy": {
                        "type": "string",
                        "default": "auto",
                        "widget": "select",
                        "label": "Document Context Strategy",
                        "helper": "How much document content to include in LLM context",
                        "options": [
                                {
                                        "value": "auto",
                                        "label": "Auto (by document size)"
                                },
                                {
                                        "value": "full",
                                        "label": "Full document text"
                                },
                                {
                                        "value": "page",
                                        "label": "Pages around cursor"
                                },
                                {
                                        "value": "tree",
                                        "label": "Outline + excerpt"
                                },
                                {
                                        "value": "stats",
                                        "label": "Stats + outline only"
                                }
                        ]
                },
                "system_prompt": {
                        "type": "string",
                        "default": "",
                        "widget": "textarea",
                        "label": "System Prompt"
                },
                "image_provider": {
                        "type": "string",
                        "default": "endpoint",
                        "widget": "select",
                        "label": "Image Provider",
                        "options": [
                                {
                                        "value": "endpoint",
                                        "label": "LLM Endpoint"
                                },
                                {
                                        "value": "ai_horde",
                                        "label": "AI Horde (free)"
                                }
                        ]
                },
                "extend_selection_max_tokens": {
                        "type": "int",
                        "default": 70,
                        "min": 10,
                        "max": 4096,
                        "widget": "number",
                        "label": "Extend Selection Max Tokens"
                },
                "edit_selection_max_new_tokens": {
                        "type": "int",
                        "default": 0,
                        "min": 0,
                        "max": 4096,
                        "widget": "number",
                        "label": "Edit Selection Extra Tokens",
                        "helper": "Extra tokens beyond original text length. 0 = same length as original."
                },
                "tool_broker": {
                        "type": "boolean",
                        "default": True,
                        "widget": "checkbox",
                        "label": "Tool Broker",
                        "helper": "Two-tier tool delivery: LLM gets core tools first, activates others by intent"
                },
                "enter_sends": {
                        "type": "boolean",
                        "default": True,
                        "widget": "checkbox",
                        "label": "Enter Sends Message",
                        "helper": "Press Enter to send, Shift+Enter for newline"
                },
                "show_mcp_activity": {
                        "type": "boolean",
                        "default": True,
                        "widget": "checkbox",
                        "label": "Show MCP Activity",
                        "public": True
                },
                "api_enabled": {
                        "type": "boolean",
                        "default": False,
                        "widget": "checkbox",
                        "label": "Enable Chat API",
                        "helper": "Expose /api/chat endpoints on the HTTP server",
                        "public": True
                },
                "api_auth_token": {
                        "type": "string",
                        "default": "",
                        "widget": "text",
                        "label": "API Auth Token",
                        "helper": "Optional Bearer token for researchers/external apps. Empty = no auth."
                },
                "query_history": {
                        "type": "string",
                        "default": "[]",
                        "internal": True
                }
        },
        "actions": [
                "extend_selection",
                "edit_selection"
        ],
        "action_icons": {}
},
    {
        "name": "tunnel",
        "title": "Tunnel providers for external MCP access",
        "requires": [
                "config",
                "events",
                "http_routes"
        ],
        "provides_services": [
                "tunnel_manager"
        ],
        "config": {
                "auto_start": {
                        "type": "boolean",
                        "default": False,
                        "widget": "checkbox",
                        "label": "Auto Start Tunnel",
                        "public": True
                },
                "provider": {
                        "type": "string",
                        "default": "",
                        "widget": "select",
                        "label": "Tunnel Provider",
                        "options_provider": "plugin.modules.tunnel:get_provider_options"
                },
                "server": {
                        "type": "string",
                        "default": "bore.pub",
                        "label": "Bore Server",
                        "helper": "Relay server (default: bore.pub)"
                },
                "tunnel_name": {
                        "type": "string",
                        "default": "",
                        "label": "Cloudflare Tunnel Name",
                        "helper": "Optional: use a named tunnel instead of a quick tunnel"
                },
                "public_url": {
                        "type": "string",
                        "default": "",
                        "label": "Cloudflare Public URL",
                        "helper": "Required for named tunnels"
                },
                "authtoken": {
                        "type": "string",
                        "default": "",
                        "widget": "password",
                        "label": "Ngrok Authtoken"
                }
        },
        "actions": [],
        "action_icons": {}
},
]

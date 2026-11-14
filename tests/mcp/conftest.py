# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from io import BytesIO
import socket
import time
from unittest.mock import MagicMock, patch

import pytest
import urllib.request

from plugin.mcp import McpModule


def get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(autouse=True)
def _isolate_mcp_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset global _current_module before each test to isolate module lifecycle."""
    import plugin.mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "_current_module", None)


@pytest.fixture(scope="module")
def mcp_server():
    """Start the HTTP server using McpModule with mocked services."""
    services = MagicMock()

    tool_registry = MagicMock()
    tool_registry.get_schemas.return_value = [
        {"name": "test_tool", "description": "A test tool", "inputSchema": {"type": "object", "properties": {}}}
    ]
    tool_registry.tool_names = ["test_tool"]
    services.tools = tool_registry

    doc_svc = MagicMock()
    doc_svc.get_active_document.return_value = MagicMock()
    doc_svc.detect_doc_type.return_value = "writer"
    doc_svc.resolve_document_by_url.return_value = (MagicMock(), "writer")
    services.document = doc_svc

    services.events = MagicMock()

    main_thread = MagicMock()

    def mock_execute(fn, *args, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("bound_scope", None)
        return fn(*args, **kwargs)

    main_thread.call.side_effect = mock_execute
    services.main_thread = main_thread
    services.get.side_effect = lambda name: getattr(services, name, None)

    import plugin.framework.config as config_mod

    old_resolved = config_mod._resolved_config_path
    if not config_mod._resolved_config_path:
        config_mod._resolved_config_path = "/tmp/writeragent.json"

    orig_get_config = config_mod.get_config
    config_values = {
        "mcp.mcp_enabled": True,
        "mcp.host": "127.0.0.1",
        "mcp.use_ssl": False,
        "mcp.ssl_cert": "",
        "mcp.ssl_key": "",
        "mcp.mcp_remote_access": False,
        "mcp.tunnel_enabled": False,
    }

    def mock_get_config(key, *args, **kwargs):
        if key in config_values:
            return config_values[key]
        return orig_get_config(key, *args, **kwargs)

    config_patcher = patch("plugin.framework.config.get_config", side_effect=mock_get_config)
    mcp_config_patcher = patch("plugin.mcp.get_config", side_effect=mock_get_config)
    config_patcher.start()
    mcp_config_patcher.start()

    http_module = None
    url = ""
    server_ready = False

    for _attempt in range(3):
        port = get_free_port()
        config_values["mcp.mcp_port"] = port

        http_module = McpModule()
        http_module.name = "mcp"
        try:
            http_module.initialize(services)
            http_module.start_background(services)
        except Exception:
            if http_module:
                http_module.shutdown()
            continue

        url = f"http://127.0.0.1:{port}"
        for _ in range(20):
            try:
                req = urllib.request.Request(f"{url}/health")
                with urllib.request.urlopen(req, timeout=1) as response:
                    if response.getcode() == 200:
                        server_ready = True
                        break
            except Exception:
                time.sleep(0.5)

        if server_ready:
            break

        http_module.shutdown()

    if not server_ready or not http_module:
        config_patcher.stop()
        mcp_config_patcher.stop()
        pytest.fail("Server did not start in time after retries")

    try:
        yield url
    finally:
        http_module.shutdown()
        config_mod._resolved_config_path = old_resolved
        config_patcher.stop()
        mcp_config_patcher.stop()


class MockHandler:
    """Mock GenericRequestHandler for testing."""

    def __init__(self, headers, body=b""):
        self.headers = headers
        self.rfile = BytesIO(body)
        self.wfile = BytesIO()
        self.client_address = ("127.0.0.1", 12345)
        self.sent_responses = []
        self.sent_headers = []
        self.headers_ended = False
        self.close_connection = False

    def send_response(self, code, message=None):
        self.sent_responses.append(code)

    def send_header(self, keyword, value):
        self.sent_headers.append((keyword, value))

    def end_headers(self):
        self.headers_ended = True



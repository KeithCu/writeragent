# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import socket
import sys
import time
from unittest.mock import MagicMock

import pytest
import urllib.request

from plugin.mcp import McpModule


def _ensure_winsock_started() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        data = ctypes.create_string_buffer(512)
        ctypes.windll.ws2_32.WSAStartup(0x0202, ctypes.byref(data))
    except Exception:
        pass


def get_free_port():
    _ensure_winsock_started()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def mcp_server():
    """Start the HTTP server using McpModule with mocked services."""
    import plugin.mcp as http_mod

    def _reset_globals():
        with http_mod._http_peer_lock:
            http_mod._primary_http_module = None
            http_mod._shared_registry = None
            http_mod._shared_http_server = None
            http_mod._shared_tunnel = None

    _reset_globals()

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
        return fn(*args, **kwargs)

    main_thread.execute.side_effect = mock_execute
    services.main_thread = main_thread
    services.get.side_effect = lambda name: getattr(services, name, None)

    http_module = None
    url = ""
    server_ready = False

    for _attempt in range(3):
        _reset_globals()
        port = get_free_port()

        config_svc = MagicMock()
        config_svc.proxy_for.return_value = {
            "enabled": True,
            "mcp_enabled": True,
            "mcp_port": port,
            "host": "127.0.0.1",
            "use_ssl": False,
        }
        services.config = config_svc

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
        _reset_globals()
        pytest.fail("Server did not start in time after retries")

    try:
        yield url
    finally:
        http_module.shutdown()
        _reset_globals()


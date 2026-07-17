# WriterAgent - Python Compute Service tests
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import base64
import json
import socket
import threading
import time
import urllib.request

import pytest

from compute_service.executor import clamp_timeout_sec, execute_code, timeout_ms_to_sec
from compute_service.json_egress import sanitize_for_strict_json, to_dumb_json_value
from compute_service.server import DualStackThreadingHTTPServer


def get_free_port() -> int:
    # Use AF_INET6 to bind if possible, fallback to AF_INET
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            return s.getsockname()[1]
    except OSError:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            return s.getsockname()[1]


@pytest.fixture(scope="module")
def compute_server_info():
    port = get_free_port()
    from compute_service.server import WSGIDualStackServer, wsgi_app
    
    server = WSGIDualStackServer("", port)
    server.set_app(wsgi_app)
    
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)
    yield port, server.srv
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def compute_url(compute_server_info):
    port, _ = compute_server_info
    return f"http://127.0.0.1:{port}"


def _post_execute(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{url}/v1/execute",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        body = json.loads(resp.read().decode("utf-8"))
        # Response must be strict JSON (no NaN tokens) — already verified by json.loads
        return body


class TestJsonEgressUnit:
    def test_nan_inf_to_null(self) -> None:
        assert sanitize_for_strict_json(float("nan")) is None
        assert sanitize_for_strict_json(float("inf")) is None
        assert sanitize_for_strict_json({"a": float("-inf"), "b": 1.5}) == {"a": None, "b": 1.5}
        # Round-trip with allow_nan=False
        json.dumps(sanitize_for_strict_json([float("nan"), 1.0]), allow_nan=False)

    def test_ndarray_to_lists(self) -> None:
        import numpy as np

        out = to_dumb_json_value(np.array([[1.0, float("nan")], [3.0, 4.0]]))
        assert out == [[1.0, None], [3.0, 4.0]]

    def test_split_grid_unpacked_to_lists(self) -> None:
        from plugin.scripting.payload_codec import child_pack_result

        import numpy as np

        packed = child_pack_result(np.arange(120).reshape(10, 12))
        assert isinstance(packed, dict) and packed.get("__wa_payload__") == "split_grid"
        out = to_dumb_json_value(packed)
        assert isinstance(out, list)
        assert len(out) == 10
        assert out[0] == list(range(12))


class TestTimeoutHelpers:
    def test_timeout_ms_rounds_up(self) -> None:
        assert timeout_ms_to_sec(1500) == 2
        assert timeout_ms_to_sec(1000) == 1
        assert timeout_ms_to_sec(0) == 30
        assert clamp_timeout_sec(99999) == 600


class TestExecuteLocal:
    def test_mode_isolated_ignores_session(self) -> None:
        sid = "iso-test-session"
        r1 = execute_code("x = 7\nresult = x", session_id=sid, mode="isolated")
        assert r1["status"] == "ok" and r1["result"] == 7
        r2 = execute_code("result = x", session_id=sid, mode="isolated")
        assert r2["status"] == "error"

    def test_mode_shared_keeps_state(self) -> None:
        sid = "shared-test-session"
        r1 = execute_code("x = 11\nresult = x", session_id=sid, mode="shared")
        assert r1["status"] == "ok" and r1["result"] == 11
        r2 = execute_code("result = x + 1", session_id=sid, mode="shared")
        assert r2["status"] == "ok" and r2["result"] == 12

    def test_large_matrix_is_nested_lists_not_split_grid(self) -> None:
        r = execute_code("import numpy as np\nresult = np.arange(120).reshape(10, 12)")
        assert r["status"] == "ok"
        assert isinstance(r["result"], list)
        assert r["result"][9][-1] == 119
        assert "__wa_payload__" not in (r["result"] if isinstance(r["result"], dict) else {})

    def test_nan_in_result_is_null(self) -> None:
        r = execute_code("result = float('nan')")
        assert r["status"] == "ok"
        assert r["result"] is None
        json.dumps(r, allow_nan=False)


class TestComputeHttp:
    def test_health(self, compute_url: str) -> None:
        with urllib.request.urlopen(f"{compute_url}/health") as resp:
            assert resp.status == 200
            assert json.loads(resp.read().decode())["status"] == "healthy"

    def test_simple_execution(self, compute_url: str) -> None:
        body = _post_execute(compute_url, {"code": "result = 3 ** 4"})
        assert body["status"] == "ok"
        assert body["result"] == 81

    def test_numpy_mean(self, compute_url: str) -> None:
        body = _post_execute(
            compute_url,
            {"code": "import numpy as np\nresult = float(np.mean(data))", "data": [10, 20, 30, 40]},
        )
        assert body["status"] == "ok"
        assert body["result"] == 25.0

    def test_error_field_not_only_message(self, compute_url: str) -> None:
        body = _post_execute(compute_url, {"code": "import os\nresult = os.name"})
        assert body["status"] == "error"
        assert "not allowed" in body.get("error", "")

    def test_ndarray_matrix_over_http(self, compute_url: str) -> None:
        body = _post_execute(
            compute_url,
            {"code": "import numpy as np\nresult = np.array([[1.0, float('nan')], [3.0, 4.0]])"},
        )
        assert body["status"] == "ok"
        assert body["result"] == [[1.0, None], [3.0, 4.0]]

    def test_matplotlib_images_top_level(self, compute_url: str) -> None:
        body = _post_execute(
            compute_url,
            {
                "code": (
                    "import matplotlib.pyplot as plt\n"
                    "fig, ax = plt.subplots()\n"
                    "ax.plot([0, 1], [0, 1])\n"
                    "result = fig"
                )
            },
        )
        assert body["status"] == "ok"
        assert body.get("result") is None
        images = body.get("images") or []
        assert len(images) == 1
        assert images[0].get("format") in ("svg", "png")
        decoded = base64.b64decode(images[0]["data_b64"])
        assert b"svg" in decoded or b"xml" in decoded or decoded[:8] == b"\x89PNG\r\n\x1a\n"

    def test_response_rejects_literal_nan_token(self, compute_url: str) -> None:
        # Server uses allow_nan=False; body was already loaded by json.loads in _post_execute
        body = _post_execute(compute_url, {"code": "result = [float('nan'), float('inf')]"})
        assert body["result"] == [None, None]

    def test_dual_stack_connectivity(self, compute_server_info) -> None:
        port, server = compute_server_info
        has_ipv6 = hasattr(server, "sockets") and any(s.family == socket.AF_INET6 for s in server.sockets)
        if not has_ipv6 and server.address_family != socket.AF_INET6:
            pytest.skip("IPv6 dual-stack not supported or fallback occurred")

        # Test IPv4 localhost
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as resp:
            assert resp.status == 200
            assert json.loads(resp.read().decode())["status"] == "healthy"

        # Test IPv6 localhost
        with urllib.request.urlopen(f"http://[::1]:{port}/health") as resp:
            assert resp.status == 200
            assert json.loads(resp.read().decode())["status"] == "healthy"

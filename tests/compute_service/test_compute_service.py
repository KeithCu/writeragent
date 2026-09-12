# WriterAgent - Python Compute Service tests
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import base64
import io
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import pytest

from compute_service.executor import clamp_timeout_sec, execute_code, timeout_ms_to_sec
from compute_service.json_egress import sanitize_for_strict_json, to_dumb_json_value
from compute_service.server import create_wsgi_app
from compute_service.config import ComputeSettings, load_settings
from plugin.version import EXTENSION_VERSION


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
    from compute_service.server import WSGIDualStackServer

    # Keyless loopback — matches local-dev default.
    app = create_wsgi_app(ComputeSettings(host="127.0.0.1", port=port))
    server = WSGIDualStackServer("", port)
    server.set_app(app)

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


def _post_execute(url: str, payload: dict, path: str = "/v1/execute") -> dict:
    if not path.startswith("/"):
        path = f"/v1/execute{path}"
    req = urllib.request.Request(
        f"{url}{path}",
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

    def test_init_script_shared_seeds_session(self) -> None:
        sid = "shared-init-session"
        r1 = execute_code(
            "result = HELPER + 1",
            session_id=sid,
            mode="shared",
            init_script="HELPER = 41",
        )
        assert r1["status"] == "ok", r1
        assert r1["result"] == 42
        r2 = execute_code("result = HELPER + 2", session_id=sid, mode="shared")
        assert r2["status"] == "ok"
        assert r2["result"] == 43

    def test_init_script_isolated_seeds_request(self) -> None:
        r = execute_code("result = HELPER", mode="isolated", init_script="HELPER = 7")
        assert r["status"] == "ok", r
        assert r["result"] == 7

    def test_init_script_runs_once_isolated(self) -> None:
        from unittest.mock import patch

        from plugin.scripting.venv import venv_sandbox as vs

        vs.clear_all_sandbox_sessions()
        init = "ONCE_ISO = 7"
        with patch.object(vs, "_run_on_executor", wraps=vs._run_on_executor) as mock_run:
            r1 = execute_code("result = ONCE_ISO", mode="isolated", init_script=init)
            r2 = execute_code("result = ONCE_ISO + 1", mode="isolated", init_script=init)
        assert r1["status"] == "ok" and r1["result"] == 7, r1
        assert r2["status"] == "ok" and r2["result"] == 8, r2
        # One init execution + two isolated cell executions (not init twice).
        assert mock_run.call_count == 3

    def test_init_script_runs_once_shared(self) -> None:
        from unittest.mock import patch

        from plugin.scripting.venv import venv_sandbox as vs

        vs.clear_all_sandbox_sessions()
        sid = "shared-init-once"
        init = "ONCE_SHARED = 10"
        with patch.object(vs, "_run_on_executor", wraps=vs._run_on_executor) as mock_run:
            r1 = execute_code("result = ONCE_SHARED", session_id=sid, mode="shared", init_script=init)
            r2 = execute_code(
                "result = ONCE_SHARED + 1",
                session_id=sid,
                mode="shared",
                init_script=init,
            )
        assert r1["status"] == "ok" and r1["result"] == 10, r1
        assert r2["status"] == "ok" and r2["result"] == 11, r2
        assert mock_run.call_count == 3


class TestComputeHttp:
    def test_health(self, compute_url: str) -> None:
        with urllib.request.urlopen(f"{compute_url}/health") as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["status"] == "healthy"
            assert data["service"] == "python-compute"
            assert data["version"] == EXTENSION_VERSION

    def test_simple_execution(self, compute_url: str) -> None:
        body = _post_execute(compute_url, {"code": "result = 3 ** 4"})
        assert body["status"] == "ok"
        assert body["result"] == 81

    def test_id_echo_on_success(self, compute_url: str) -> None:
        body = _post_execute(compute_url, {"id": "test-req-1", "code": "result = 42"})
        assert body["status"] == "ok"
        assert body["result"] == 42
        assert body.get("id") == "test-req-1"

    def test_id_echo_on_execution_error(self, compute_url: str) -> None:
        body = _post_execute(compute_url, {"id": "test-req-err", "code": "import os\nresult = os.name"})
        assert body["status"] == "error"
        assert body.get("id") == "test-req-err"

    def test_id_echo_on_bad_request(self, compute_url: str) -> None:
        req = urllib.request.Request(
            f"{compute_url}/v1/execute",
            data=json.dumps({"id": "test-bad-req", "code": 12345}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 400
        body = json.loads(exc_info.value.read().decode("utf-8"))
        assert body.get("id") == "test-bad-req"
        assert body.get("status") == "error"

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

    def test_shared_session_via_query_param(self, compute_url: str) -> None:
        sid = "http-query-session-1"
        r1 = _post_execute(
            compute_url,
            {"code": "x = 42\nresult = x", "mode": "shared"},
            path=f"?session_id={sid}",
        )
        assert r1["status"] == "ok" and r1["result"] == 42
        r2 = _post_execute(
            compute_url,
            {"code": "result = x + 8", "mode": "shared"},
            path=f"?session_id={sid}",
        )
        assert r2["status"] == "ok" and r2["result"] == 50

    def test_url_encoded_query_param(self, compute_url: str) -> None:
        r1 = _post_execute(
            compute_url,
            {"code": "x = 99\nresult = x", "mode": "shared"},
            path="?session_id=my%20doc%202026",
        )
        assert r1["status"] == "ok" and r1["result"] == 99
        r2 = _post_execute(
            compute_url,
            {"code": "result = x", "mode": "shared"},
            path="?session_id=my%20doc%202026",
        )
        assert r2["status"] == "ok" and r2["result"] == 99

    def test_session_id_in_json_body_returns_400(self, compute_url: str) -> None:
        req = urllib.request.Request(
            f"{compute_url}/v1/execute",
            data=json.dumps({"id": "body-sid-req", "code": "result = 1", "session_id": "bad-body-sid"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 400
        body = json.loads(exc_info.value.read().decode("utf-8"))
        assert body.get("id") == "body-sid-req"
        assert body.get("status") == "error"
        assert "query parameter" in body.get("error", "")

    def test_shared_mode_missing_session_id_returns_400(self, compute_url: str) -> None:
        req = urllib.request.Request(
            f"{compute_url}/v1/execute",
            data=json.dumps({"id": "missing-sid-req", "code": "result = 1", "mode": "shared"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 400
        body = json.loads(exc_info.value.read().decode("utf-8"))
        assert body.get("id") == "missing-sid-req"
        assert body.get("status") == "error"
        assert "requires a 'session_id' URL query parameter" in body.get("error", "")

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


class TestComputeSettings:
    def test_log_level_default_and_custom(self) -> None:
        s1 = load_settings(environ={})
        assert s1.log_level == "INFO"

        s2 = load_settings(environ={"PYTHON_COMPUTE_LOG_LEVEL": "debug"})
        assert s2.log_level == "DEBUG"

        s3 = load_settings(environ={"PYTHON_COMPUTE_LOG_LEVEL": "warn"})
        assert s3.log_level == "WARNING"

        s4 = load_settings(environ={"PYTHON_COMPUTE_LOG_LEVEL": "WARNING"})
        assert s4.log_level == "WARNING"

        with pytest.raises(Exception) as exc_info:
            load_settings(environ={"PYTHON_COMPUTE_LOG_LEVEL": "INVALID_LEVEL"})
        assert "Invalid log_level" in str(exc_info.value)
    def test_keyless_ok(self) -> None:
        s = load_settings(environ={"PYTHON_COMPUTE_HOST": "127.0.0.1", "PYTHON_COMPUTE_PORT": "8000"})
        assert s.host == "127.0.0.1"
        assert not s.auth_required

    def test_wildcard_without_key_is_insecure_ok(self) -> None:
        s = load_settings(environ={"PYTHON_COMPUTE_HOST": "0.0.0.0", "PYTHON_COMPUTE_PORT": "8000"})
        assert s.host == "0.0.0.0"
        assert not s.auth_required

    def test_env_api_key_and_host(self) -> None:
        s = load_settings(
            environ={
                "PYTHON_COMPUTE_HOST": "0.0.0.0",
                "PYTHON_COMPUTE_PORT": "9001",
                "PYTHON_COMPUTE_API_KEY": "secret-token",
            }
        )
        assert s.host == "0.0.0.0"
        assert s.port == 9001
        assert s.api_key == "secret-token"
        assert s.auth_required

    def test_key_file_strips_trailing_newline(self, tmp_path) -> None:
        key_path = tmp_path / "key"
        key_path.write_text("abc123\n", encoding="utf-8")
        s = load_settings(api_key_file=key_path, environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})
        assert s.api_key == "abc123"

    def test_cli_key_file_beats_env_key(self, tmp_path) -> None:
        key_path = tmp_path / "key"
        key_path.write_text("from-file", encoding="utf-8")
        s = load_settings(
            api_key_file=key_path,
            environ={"PYTHON_COMPUTE_API_KEY": "from-env", "PYTHON_COMPUTE_HOST": "127.0.0.1"},
        )
        assert s.api_key == "from-file"

    def test_config_json_nested(self, tmp_path) -> None:
        cfg = tmp_path / "python-compute.json"
        key_path = tmp_path / "secret"
        key_path.write_text("json-secret", encoding="utf-8")
        cfg.write_text(
            json.dumps(
                {
                    "listen": {"host": "127.0.0.1", "port": 8123},
                    "auth": {"api_key_file": str(key_path)},
                    "limits": {"max_body_bytes": 4096, "default_timeout_sec": 12, "shared_kernel_ttl_sec": 1800.0},
                }
            ),
            encoding="utf-8",
        )
        s = load_settings(config_path=cfg, environ={})
        assert s.port == 8123
        assert s.api_key == "json-secret"
        assert s.max_body_bytes == 4096
        assert s.default_timeout_sec == 12
        assert s.shared_kernel_ttl_sec == 1800.0

    def test_shared_kernel_ttl_env(self) -> None:
        s = load_settings(environ={"PYTHON_COMPUTE_SHARED_KERNEL_TTL_SEC": "7200.0", "PYTHON_COMPUTE_HOST": "127.0.0.1"})
        assert s.shared_kernel_ttl_sec == 7200.0

    def test_cli_host_overrides_config(self, tmp_path) -> None:
        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps({"listen": {"host": "0.0.0.0", "port": 8000}}), encoding="utf-8")
        s = load_settings(config_path=cfg, host="127.0.0.1", environ={})
        assert s.host == "127.0.0.1"
        assert not s.auth_required

    def test_max_threads_env_and_cli(self) -> None:
        s = load_settings(environ={"PYTHON_COMPUTE_MAX_THREADS": "8", "PYTHON_COMPUTE_HOST": "127.0.0.1"})
        assert s.threads == 8
        assert s.max_threads == 8
        assert s.workers == 1  # default
        s2 = load_settings(threads=4, workers=3, environ={"PYTHON_COMPUTE_MAX_THREADS": "8", "PYTHON_COMPUTE_HOST": "127.0.0.1"})
        assert s2.threads == 4
        assert s2.workers == 3

    def test_workers_env_and_cli(self) -> None:
        s = load_settings(environ={"PYTHON_COMPUTE_WORKERS": "5", "PYTHON_COMPUTE_HOST": "127.0.0.1"})
        assert s.workers == 5
        assert s.threads == 2  # default
        s2 = load_settings(workers=1, environ={"PYTHON_COMPUTE_WORKERS": "5", "PYTHON_COMPUTE_HOST": "127.0.0.1"})
        assert s2.workers == 1

    def test_threads_and_workers_json(self, tmp_path) -> None:
        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps({"limits": {"threads": 24, "workers": 4}}), encoding="utf-8")
        s = load_settings(config_path=cfg, environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})
        assert s.threads == 24
        assert s.workers == 4

    def test_max_threads_json(self, tmp_path) -> None:
        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps({"limits": {"max_threads": 12}}), encoding="utf-8")
        s = load_settings(config_path=cfg, environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})
        assert s.threads == 12

    def test_threads_and_workers_invalid(self) -> None:
        from compute_service.config import ConfigError

        with pytest.raises(ConfigError):
            load_settings(threads=0, environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})
        with pytest.raises(ConfigError):
            load_settings(workers=0, environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})

    def test_negative_shared_kernel_ttl_rejected(self, tmp_path) -> None:
        """Negative shared_kernel_ttl_sec must be rejected by validate()."""
        from compute_service.config import ConfigError

        cfg = tmp_path / "neg_ttl.json"
        cfg.write_text(json.dumps({"limits": {"shared_kernel_ttl_sec": -1}}), encoding="utf-8")
        with pytest.raises(ConfigError, match="shared_kernel_ttl_sec"):
            load_settings(config_path=cfg, environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})

    def test_negative_idle_worker_ttl_rejected(self, tmp_path) -> None:
        """Negative idle_worker_ttl_sec must be rejected by validate()."""
        from compute_service.config import ConfigError

        cfg = tmp_path / "neg_idle_ttl.json"
        cfg.write_text(json.dumps({"limits": {"idle_worker_ttl_sec": -5}}), encoding="utf-8")
        with pytest.raises(ConfigError, match="idle_worker_ttl_sec"):
            load_settings(config_path=cfg, environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})

    def test_key_file_preserves_leading_and_trailing_spaces(self, tmp_path) -> None:
        """_read_key_file must NOT strip() the key; only the one trailing newline is removed.
        API keys with leading/trailing spaces (unusual but valid) must round-trip intact."""
        key_path = tmp_path / "key_spaces"
        # Leading space, no trailing newline
        key_path.write_bytes(b" abc123 ")
        s = load_settings(api_key_file=key_path, environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})
        assert s.api_key == " abc123 "

    def test_key_file_strips_only_one_trailing_newline(self, tmp_path) -> None:
        """Verify that only the single trailing newline is removed, not all whitespace."""
        key_path = tmp_path / "key_nl"
        key_path.write_bytes(b"mykey\n")
        s = load_settings(api_key_file=key_path, environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})
        assert s.api_key == "mykey"


class TestBearerAuthHttp:
    @pytest.fixture(scope="class")
    def auth_server(self):
        """One HTTP server for the class; clear `executed` between tests via autouse below."""
        port = get_free_port()
        from compute_service.server import WSGIDualStackServer

        executed: list[str] = []

        def fake_execute(**kwargs):
            executed.append(kwargs["code"])
            return {"status": "ok", "result": 1, "stdout": ""}

        settings = ComputeSettings(host="127.0.0.1", port=port, api_key="correct-secret")
        app = create_wsgi_app(settings, execute_fn=fake_execute)
        server = WSGIDualStackServer("127.0.0.1", port)
        server.set_app(app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.15)
        yield f"http://127.0.0.1:{port}", executed
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    @pytest.fixture(autouse=True)
    def _clear_executed(self, auth_server):
        _url, executed = auth_server
        executed.clear()
        yield

    def _post(self, url: str, headers: dict[str, str] | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"{url}/v1/execute",
            data=json.dumps({"code": "result = 1"}).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8")
            return err.code, json.loads(body) if body else {}

    def test_health_public(self, auth_server) -> None:
        url, executed = auth_server
        with urllib.request.urlopen(f"{url}/health") as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["status"] == "healthy"
            assert data["service"] == "python-compute"
            assert data["version"] == EXTENSION_VERSION
        assert executed == []

    def test_correct_bearer(self, auth_server) -> None:
        url, executed = auth_server
        status, body = self._post(url, {"Authorization": "Bearer correct-secret"})
        assert status == 200
        assert body["result"] == 1
        assert executed == ["result = 1"]

    def test_missing_bearer(self, auth_server) -> None:
        url, executed = auth_server
        status, body = self._post(url)
        assert status == 401
        assert body.get("status") == "error"
        assert executed == []

    def test_wrong_bearer(self, auth_server) -> None:
        url, executed = auth_server
        status, body = self._post(url, {"Authorization": "Bearer wrong"})
        assert status == 401
        assert executed == []

    def test_malformed_bearer(self, auth_server) -> None:
        url, executed = auth_server
        status, _body = self._post(url, {"Authorization": "bearer correct-secret"})
        assert status == 401
        assert executed == []

    def test_www_authenticate_header(self, auth_server) -> None:
        url, _ = auth_server
        req = urllib.request.Request(
            f"{url}/v1/execute",
            data=b'{"code":"result=1"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req)
        assert ei.value.headers.get("WWW-Authenticate") == "Bearer"

    def test_hmac_rejects_tokens_of_different_length(self, auth_server) -> None:
        """Removing the len() pre-check means compare_digest is always called.
        Tokens of any length that don't match must still be rejected with 401."""
        url, executed = auth_server
        # shorter, longer, empty — all must be rejected
        for bad_token in ["x", "correct-secret-plus-extra", ""]:
            status, body = self._post(url, {"Authorization": f"Bearer {bad_token}"})
            assert status == 401, f"Expected 401 for token {bad_token!r}, got {status}"
            assert body.get("status") == "error"
        assert executed == []


class TestRequestBodyLimits:
    def test_negative_content_length_is_400(self) -> None:
        app = create_wsgi_app(
            ComputeSettings(),
            execute_fn=lambda **_kw: {"status": "ok", "result": 1, "stdout": ""},
        )
        status_holder: list[str] = []

        def start_response(status: str, _headers: list) -> None:
            status_holder.append(status)

        environ = {
            "PATH_INFO": "/v1/execute",
            "REQUEST_METHOD": "POST",
            "CONTENT_LENGTH": "-1",
            "wsgi.input": io.BytesIO(b'{"code":"result=1"}' * 5000),
        }
        body = b"".join(app(environ, start_response))
        assert status_holder[0].startswith("400")
        assert json.loads(body)["status"] == "error"

    def test_truncated_body_returns_400(self) -> None:
        """wsgi.input.read() returning fewer bytes than CONTENT_LENGTH must yield 400, not a
        misleading 'Invalid JSON' error."""
        app = create_wsgi_app(
            ComputeSettings(),
            execute_fn=lambda **_kw: {"status": "ok", "result": 1, "stdout": ""},
        )
        status_holder: list[str] = []

        def start_response(status: str, _headers: list) -> None:
            status_holder.append(status)

        real_body = json.dumps({"code": "result = 1"}).encode("utf-8")
        # Report more bytes than we actually provide
        truncated = real_body[: len(real_body) // 2]
        environ = {
            "PATH_INFO": "/v1/execute",
            "REQUEST_METHOD": "POST",
            "CONTENT_LENGTH": str(len(real_body)),
            "wsgi.input": io.BytesIO(truncated),
        }
        body = b"".join(app(environ, start_response))
        assert status_holder[0].startswith("400")
        parsed = json.loads(body)
        assert parsed["status"] == "error"
        assert "truncated" in parsed["error"]

    def test_vision_unhandled_exception_is_json_500(self) -> None:
        fake_pool = MagicMock()
        fake_pool.execute.side_effect = RuntimeError("vision boom")
        app = create_wsgi_app(ComputeSettings())
        status_holder: list[str] = []

        def start_response(status: str, _headers: list) -> None:
            status_holder.append(status)

        payload = json.dumps({"id": "v-x", "image_b64": "YQ=="}).encode("utf-8")
        environ = {
            "PATH_INFO": "/v1/vision",
            "REQUEST_METHOD": "POST",
            "CONTENT_LENGTH": str(len(payload)),
            "wsgi.input": io.BytesIO(payload),
        }
        with patch("compute_service.vision_pool.get_vision_pool", return_value=fake_pool):
            body = b"".join(app(environ, start_response))
        assert status_holder[0].startswith("500")
        parsed = json.loads(body)
        assert parsed["status"] == "error"
        assert parsed.get("id") == "v-x"
        assert "vision boom" in parsed["error"]


class TestImportBoundary:
    def test_config_auth_startup_avoids_writeragent_config(self) -> None:
        """Config + auth app construction must not import plugin.framework.config
        or open writeragent.json (executor sandbox coupling is deferred to first execute).
        """
        import subprocess
        import sys
        from pathlib import Path

        repo = Path(__file__).resolve().parents[2]
        code = r"""
import builtins
import sys
from pathlib import Path

opened = []
_real_open = builtins.open

def _tracking_open(file, *args, **kwargs):
    path = Path(file) if not isinstance(file, Path) else file
    opened.append(str(path))
    return _real_open(file, *args, **kwargs)

builtins.open = _tracking_open

from compute_service.config import load_settings
from compute_service.server import authenticate_request, create_wsgi_app

s = load_settings(environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})
app = create_wsgi_app(s)
principal, err = authenticate_request({}, s)
assert principal == "default" and err is None
assert "plugin.framework.config" not in sys.modules
assert not any(Path(p).name == "writeragent.json" for p in opened), opened
print("ok")
"""
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert "ok" in proc.stdout

    def test_server_startup_does_not_import_numpy_or_sympy(self) -> None:
        """Master compute service server must not load heavy packages (numpy, sympy) into memory."""
        import subprocess
        import sys
        from pathlib import Path

        repo = Path(__file__).resolve().parents[2]
        code = r"""
import sys
from compute_service.config import load_settings
from compute_service.server import create_wsgi_app, WSGIDualStackServer

s = load_settings(environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})
app = create_wsgi_app(s)
assert "numpy" not in sys.modules, f"numpy was loaded into master process: {sys.modules.get('numpy')}"
assert "sympy" not in sys.modules, f"sympy was loaded into master process: {sys.modules.get('sympy')}"
print("ok")
"""
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert "ok" in proc.stdout

    def test_check_dependencies_exit_on_failure(self, monkeypatch, capsys) -> None:
        """check_dependencies should print error and exit with code 1 if worker pool reports failure."""
        from compute_service.server import check_dependencies

        mock_pool = MagicMock()
        mock_pool.check_dependencies.return_value = (False, "Error: fake_pkg is not installed")

        with pytest.raises(SystemExit) as exc_info:
            check_dependencies(mock_pool)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Error: fake_pkg is not installed" in captured.err


# WriterAgent - Python Compute Service tests
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import base64
import io
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from compute_service.config import ComputeSettings, ConfigError, clamp_timeout_sec, load_settings
from compute_service.executor import execute_code
from compute_service.formula_pool import shutdown_formula_pool
from compute_service.json_egress import normalize_execute_response, sanitize_for_strict_json, to_dumb_json_value
from compute_service.server import create_wsgi_app
from compute_service.vision_pool import shutdown_vision_pool


def _wsgi_post(
    app,
    body: bytes | None,
    *,
    path: str = "/v1/session/reset",
    query: str = "",
    headers: dict[str, str] | None = None,
) -> tuple[str, list[tuple[str, str]], dict]:
    status_holder: list[str] = []
    header_holder: list[tuple[str, str]] = []

    def start_response(status: str, resp_headers: list) -> None:
        status_holder.append(status)
        header_holder.extend(resp_headers)

    payload = b"" if body is None else body
    environ: dict = {
        "PATH_INFO": path,
        "REQUEST_METHOD": "POST",
        "QUERY_STRING": query,
        "wsgi.input": io.BytesIO(payload),
    }
    if body is None:
        environ["CONTENT_LENGTH"] = ""
    else:
        environ["CONTENT_LENGTH"] = str(len(payload))
    if headers:
        for key, value in headers.items():
            env_key = "HTTP_" + key.upper().replace("-", "_")
            if key.lower() == "content-type":
                environ["CONTENT_TYPE"] = value
            else:
                environ[env_key] = value
    out = b"".join(app(environ, start_response))
    parsed = json.loads(out.decode("utf-8")) if out else {}
    return status_holder[0], header_holder, parsed
from plugin.version import EXTENSION_VERSION
from tests.compute_service.conftest import get_free_port


@pytest.fixture(scope="module")
def compute_server_info():
    port = get_free_port()
    from compute_service.server import WSGIDualStackServer

    # Keyless loopback — matches local-dev default. Extra workers so the
    # shared sessions this module keeps do not occupy every process;
    # isolated calls are not allowed to run on those processes.
    app = create_wsgi_app(ComputeSettings(host="127.0.0.1", port=port, workers=4))
    server = WSGIDualStackServer("", port)
    server.set_app(app)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)
    yield port, server.srv
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    # The first /v1/execute builds the process-global pool. Stop it here so
    # a later module on this worker does not reuse those children.
    shutdown_formula_pool()
    shutdown_vision_pool()


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

    def test_nested_image_is_not_copied_into_result(self) -> None:
        img = {"__wa_payload__": "image", "format": "png", "data": b"\x89PNG"}
        out = normalize_execute_response({"status": "ok", "result": {"title": "t", "plot": img}, "stdout": ""})
        assert out["status"] == "ok"
        images = out.get("images") or []
        assert len(images) == 1
        assert images[0]["format"] == "png"
        assert images[0]["data_b64"]
        assert out["result"] == {"title": "t", "plot": None}
        assert "data_b64" not in json.dumps(out["result"])

    def test_image_past_finder_depth_stays_inline(self) -> None:
        """find_image_payloads stops at depth 12. A deeper plot must not become null.

        drop used to be a bool: any listed image nulled every image, including
        ones the finder never returned, so those plots vanished from both
        result and images. Depth is counted from the result root. The value
        under ``buried`` is one level down, so 12 wrappers put the plot at
        depth 13 (missed) and 11 wrappers put it at depth 12 (listed).
        """
        from plugin.scripting.payload_codec import find_image_payloads

        def _wrap(obj: dict[str, Any], levels: int) -> dict[str, Any]:
            wrapped = obj
            for _idx in range(levels):
                wrapped = {"n": wrapped}
            return wrapped

        shallow = {"__wa_payload__": "image", "format": "png", "data": b"shallow"}
        deep = {"__wa_payload__": "image", "format": "png", "data": b"deep-png"}
        missed = _wrap(deep, 12)
        listed_nest = _wrap(deep, 11)
        missed_tree = {"plot": shallow, "buried": missed}
        listed_tree = {"plot": shallow, "buried": listed_nest}
        assert [img["data"] for img in find_image_payloads(missed_tree)] == [b"shallow"]
        assert [img["data"] for img in find_image_payloads(listed_tree)] == [b"shallow", b"deep-png"]

        out = normalize_execute_response({"status": "ok", "result": missed_tree, "stdout": ""})
        assert len(out.get("images") or []) == 1
        assert out["result"]["plot"] is None
        node = out["result"]["buried"]
        for _idx in range(12):
            assert isinstance(node, dict)
            node = node["n"]
        assert node.get("format") == "png"
        assert node.get("data_b64")

        listed = normalize_execute_response({"status": "ok", "result": listed_tree, "stdout": ""})
        assert len(listed.get("images") or []) == 2
        leaf = listed["result"]["buried"]
        for _idx in range(11):
            leaf = leaf["n"]
        assert leaf is None

    def test_unlisted_image_dict_is_not_nulled(self) -> None:
        """A plot the finder did not return stays in result when another plot is listed."""
        listed = {"__wa_payload__": "image", "format": "png", "data": b"png"}
        # str data fails is_image_payload, so find_image_payloads skips it.
        unlisted = {"__wa_payload__": "image", "format": "png", "data": "not-bytes"}
        out = normalize_execute_response({"status": "ok", "result": {"a": listed, "b": unlisted}, "stdout": ""})
        assert len(out.get("images") or []) == 1
        assert out["result"]["a"] is None
        assert out["result"]["b"] == {"format": "png", "data_b64": "not-bytes"}


class TestTimeoutHelpers:
    def test_timeout_ms_rounds_up(self) -> None:
        assert clamp_timeout_sec(1500, is_ms=True) == 2
        assert clamp_timeout_sec(1000, is_ms=True) == 1
        assert clamp_timeout_sec(0, is_ms=True) == 30
        assert clamp_timeout_sec(float("inf"), is_ms=True) == 30
        assert clamp_timeout_sec(float("-inf"), is_ms=True) == 30
        assert clamp_timeout_sec(99999) == 600

    def test_max_timeout_sec_1800_honored(self) -> None:
        assert clamp_timeout_sec(1200, max_timeout_sec=1800) == 1200
        assert clamp_timeout_sec(99999, max_timeout_sec=1800) == 1800
        res = execute_code("result = 42", timeout_sec=1200, max_timeout_sec=1800)
        assert res["status"] == "ok"
        assert res["result"] == 42
        res2 = execute_code("result = 43", timeout_sec=1200)
        assert res2["status"] == "ok"
        assert res2["result"] == 43


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

    def test_health_answers_while_listener_thread_is_blocked(self) -> None:
        """A cell holds the only listener thread. /health must still return.

        The old path peeked the socket and, on a miss, queued the probe on
        the same pool the cell occupies.
        """
        from compute_service.server import WSGIDualStackServer

        port = get_free_port()
        hold = threading.Event()
        started = threading.Event()

        def execute_fn(**_kwargs):
            started.set()
            assert hold.wait(timeout=10)
            return {"status": "ok", "result_json": b'{"status":"ok","result":1,"stdout":""}'}

        app = create_wsgi_app(ComputeSettings(host="127.0.0.1", port=port, workers=1), execute_fn=execute_fn)
        server = WSGIDualStackServer("127.0.0.1", port, max_threads=1)
        server.set_app(app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.15)
        poster: threading.Thread | None = None
        try:
            def _post() -> None:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/execute",
                    data=json.dumps({"code": "result = 1"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    assert resp.status == 200

            poster = threading.Thread(target=_post)
            poster.start()
            assert started.wait(timeout=5)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
                assert resp.status == 200
                assert json.loads(resp.read().decode())["status"] == "healthy"
        finally:
            hold.set()
            if poster is not None:
                poster.join(timeout=5)
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_health_never_starved_when_worker_semaphore_is_saturated(self) -> None:
        """When calculation requests saturate the worker semaphore, /health must still respond immediately.

        The worker semaphore limits concurrent worker-waiting requests to worker count via
        non-blocking admission before request bodies are read. Requests waiting for a worker
        do not hold HTTP listener threads, guaranteeing that spare listener threads remain
        strictly free for health probes even when incoming requests exceed pool thread capacity.
        """
        from compute_service.server import WSGIDualStackServer

        port = get_free_port()
        hold = threading.Event()
        started = threading.Event()

        def execute_fn(**_kwargs: Any) -> dict[str, Any]:
            started.set()
            assert hold.wait(timeout=10)
            return {"status": "ok", "result_json": b'{"status":"ok","result":1,"stdout":""}'}

        # 1 worker, semaphore size 1. Explicit max_threads=1 is listener_thread_count:
        # max(8, n + 4) = 8. A semaphore miss is a fast 503 and does not hold a thread.
        sem = threading.Semaphore(1)
        app = create_wsgi_app(ComputeSettings(host="127.0.0.1", port=port, workers=1), execute_fn=execute_fn, worker_semaphore=sem)
        server = WSGIDualStackServer("127.0.0.1", port, max_threads=1)
        server.set_app(app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.15)
        posters: list[threading.Thread] = []
        results: list[list[Any]] = [[] for _ in range(6)]
        try:
            def _post(out_list: list[Any]) -> None:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/execute",
                    data=json.dumps({"code": "result = 1"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        out_list.append((resp.status, json.loads(resp.read().decode())))
                except urllib.error.HTTPError as exc:
                    out_list.append((exc.code, json.loads(exc.read().decode())))
                except Exception as exc:
                    out_list.append((599, str(exc)))

            # Six posters, one worker permit. Listener threads are max(8, 1 + 4) = 8.
            # The semaphore is non-blocking: a miss is a fast 503 and does not hold
            # a listener. Health must still return, and the five executes that miss
            # the permit must 503.
            for idx in range(6):
                p = threading.Thread(target=_post, args=(results[idx],))
                p.start()
                posters.append(p)

            assert started.wait(timeout=5)
            # The active calculation is still held. Verify that /health responds immediately (<0.5s).
            t0 = time.perf_counter()
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
                t_elapsed = time.perf_counter() - t0
                assert resp.status == 200
                assert json.loads(resp.read().decode())["status"] == "healthy"
                assert t_elapsed < 0.5, f"/health took too long: {t_elapsed:.3f}s"

            # The permit is taken inside the handler, before the body is read, and only
            # by routes that wait on a worker. Releasing the in-flight execute first
            # let accepts still sitting in the backlog or the listener queue take that
            # permit and return 200 (CI: six 200s, no WORKER_POOL_BUSY). One of the six
            # calls is inside execute and cannot finish until hold is set, so the other
            # five responses have to arrive as 503s while the permit is still held.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and sum(1 for item in results if item) < 5:
                time.sleep(0.01)
            shed = [item[0] for item in results if item]
            assert len(shed) >= 5, f"overflow was not rejected while the worker was held: {results!r}"
            assert all(status == 503 and isinstance(body, dict) and body.get("code") == "WORKER_POOL_BUSY" for status, body in shed)
        finally:
            hold.set()
            for p in posters:
                p.join(timeout=5)
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        # 1 active calculation succeeded (200), and 5 excess requests received fast 503 WORKER_POOL_BUSY
        statuses = [r[0][0] for r in results if r]
        assert statuses.count(200) == 1
        assert statuses.count(503) == 5
        busy_codes = [r[0][1].get("code") for r in results if r and r[0][0] == 503]
        assert all(c == "WORKER_POOL_BUSY" for c in busy_codes)

    def test_slow_upload_rejected_fast_when_workers_busy(self) -> None:
        """When workers are busy, incoming requests are rejected before reading the body.

        A slow client streaming a large request body does not occupy an HTTP listener
        thread or block /health when all workers are leased.
        """
        from compute_service.server import WSGIDualStackServer

        port = get_free_port()
        hold = threading.Event()
        started = threading.Event()

        def execute_fn(**_kwargs: Any) -> dict[str, Any]:
            started.set()
            assert hold.wait(timeout=10)
            return {"status": "ok", "result_json": b'{"status":"ok","result":1,"stdout":""}'}

        sem = threading.Semaphore(1)
        app = create_wsgi_app(ComputeSettings(host="127.0.0.1", port=port, workers=1), execute_fn=execute_fn, worker_semaphore=sem)
        server = WSGIDualStackServer("127.0.0.1", port, max_threads=1)
        server.set_app(app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.15)
        poster: threading.Thread | None = None
        try:
            def _post() -> None:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/execute",
                    data=json.dumps({"code": "result = 1"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=15):
                        pass
                except Exception:
                    pass

            poster = threading.Thread(target=_post)
            poster.start()
            assert started.wait(timeout=5)

            # Send headers with Content-Length: 1000000 but send no body data.
            # Because semaphore is checked before body read, server responds 503 immediately.
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect(("127.0.0.1", port))
            req_headers = (
                b"POST /v1/execute HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 1000000\r\n\r\n"
            )
            sock.sendall(req_headers)
            chunks: list[bytes] = []
            while True:
                try:
                    c = sock.recv(4096)
                    if not c:
                        break
                    chunks.append(c)
                except OSError:
                    break
            sock.close()
            full_resp = b"".join(chunks)

            assert b"503 Service Unavailable" in full_resp
            assert b"WORKER_POOL_BUSY" in full_resp

            # /health remains immediately responsive
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
                assert resp.status == 200
                assert json.loads(resp.read().decode())["status"] == "healthy"
        finally:
            hold.set()
            if poster is not None:
                poster.join(timeout=5)
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

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

    @pytest.mark.parametrize(
        "value, value_2, value_3, expected, value_4",
        [
            pytest.param("session_id", "body-sid-req", "bad-body-sid", "body-sid-req", "query parameter", id="test_session_id_in_json_body_returns_400"),
            pytest.param("mode", "missing-sid-req", "shared", "missing-sid-req", "requires a 'session_id' URL query parameter", id="test_shared_mode_missing_session_id_returns_400"),
        ],
    )
    def test_session_id_in_json_body_returns_400(self, compute_url: str, value, value_2, value_3, expected, value_4) -> None:
        req = urllib.request.Request(
            f"{compute_url}/v1/execute",
            data=json.dumps({"id": value_2, "code": "result = 1", value: value_3}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 400
        body = json.loads(exc_info.value.read().decode("utf-8"))
        assert body.get("id") == expected
        assert body.get("status") == "error"
        assert value_4 in body.get("error", "")

    def test_session_reset_clears_shared_names(self, compute_url: str) -> None:
        """After shared executes, reset drops the kernel so later shared code cannot see prior names."""
        sid = "http-reset-session-1"
        r1 = _post_execute(
            compute_url,
            {"code": "prior_name = 7\nresult = prior_name", "mode": "shared"},
            path=f"?session_id={sid}",
        )
        assert r1["status"] == "ok" and r1["result"] == 7

        req = urllib.request.Request(
            f"{compute_url}/v1/session/reset?session_id={sid}",
            data=json.dumps({"id": "reset-corr"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
            reset_body = json.loads(resp.read().decode("utf-8"))
        assert reset_body.get("status") == "ok"
        assert reset_body.get("id") == "reset-corr"

        r2 = _post_execute(
            compute_url,
            {"code": "result = prior_name", "mode": "shared"},
            path=f"?session_id={sid}",
        )
        assert r2["status"] == "error"
        assert "prior_name" in r2.get("error", "") or "NameError" in r2.get("error", "")


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

    def test_settings_repr_hides_api_key(self) -> None:
        s = ComputeSettings(api_key="super-secret-key")
        r = repr(s)
        assert "super-secret-key" not in r
        assert "api_key" not in r

    def test_keyless_cors_and_dns_rebinding(self) -> None:
        app = create_wsgi_app(
            ComputeSettings(),
            reset_fn=lambda sid, **_kw: {"status": "ok"},
        )

        # Origin header gets 403
        status, headers, body = _wsgi_post(app, b"{}", headers={"Origin": "http://evil.example"})
        assert status.startswith("403")
        assert body.get("code") == "CROSS_ORIGIN_REFUSED"

        # Host header not loopback gets 403
        status, headers, body = _wsgi_post(app, b"{}", headers={"Host": "evil.example:8000"})
        assert status.startswith("403")
        assert body.get("code") == "CROSS_ORIGIN_REFUSED"

        # No Origin, loopback Host gets 400 (which means it passed the CORS pre-check and failed on empty body execute)
        status, headers, body = _wsgi_post(app, b"{}", headers={"Host": "127.0.0.1:8000"})
        assert status.startswith("400")

        # No Origin and no Host gets 400 (allows absent Host)
        status, headers, body = _wsgi_post(app, b"{}", headers={})
        assert status.startswith("400")
        assert body.get("code") != "CROSS_ORIGIN_REFUSED"

    def test_keyed_cors_and_dns_rebinding_allowed(self) -> None:
        app = create_wsgi_app(
            ComputeSettings(api_key="secret"),
            reset_fn=lambda sid, **_kw: {"status": "ok"},
        )

        # Valid key, cross-origin/non-loopback host is accepted (returns 400 because body is empty, not 403 CORS)
        status, headers, body = _wsgi_post(
            app,
            b"{}",
            headers={
                "Origin": "http://evil.example",
                "Host": "evil.example:8000",
                "Authorization": "Bearer secret"
            }
        )
        assert status.startswith("400")
        assert body.get("code") != "CROSS_ORIGIN_REFUSED"

    def test_wildcard_without_key_is_rejected(self) -> None:
        """A non-loopback bind without a key must fail inside load_settings."""
        with pytest.raises(ConfigError, match="API key"):
            load_settings(environ={"PYTHON_COMPUTE_HOST": "0.0.0.0", "PYTHON_COMPUTE_PORT": "8000"})
        with pytest.raises(ConfigError, match="API key"):
            load_settings(environ={"PYTHON_COMPUTE_HOST": "::"})
        with pytest.raises(ConfigError, match="API key"):
            ComputeSettings(host="0.0.0.0")

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

    def test_raw_api_key_in_json_fails_closed(self, tmp_path) -> None:
        cfg = tmp_path / "python-compute.json"
        cfg.write_text(json.dumps({"auth": {"api_key": "from-json"}}), encoding="utf-8")
        with pytest.raises(ConfigError, match="api_key"):
            load_settings(config_path=cfg, environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})
        cfg.write_text(json.dumps({"api_key": "top-level"}), encoding="utf-8")
        with pytest.raises(ConfigError, match="api_key"):
            load_settings(config_path=cfg, environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})

    def test_shared_kernel_ttl_env(self) -> None:
        s = load_settings(environ={"PYTHON_COMPUTE_SHARED_KERNEL_TTL_SEC": "7200.0", "PYTHON_COMPUTE_HOST": "127.0.0.1"})
        assert s.shared_kernel_ttl_sec == 7200.0

    def test_cli_host_overrides_config(self, tmp_path) -> None:
        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps({"listen": {"host": "0.0.0.0", "port": 8000}}), encoding="utf-8")
        s = load_settings(config_path=cfg, host="127.0.0.1", environ={})
        assert s.host == "127.0.0.1"
        assert not s.auth_required

    def test_env_overrides_json_config(self, tmp_path) -> None:
        """Environment variables must take precedence over JSON config values (#1365)."""
        cfg = tmp_path / "cfg.json"
        cfg.write_text(
            json.dumps({
                "port": 8000,
                "limits": {"default_timeout_sec": 15, "workers": 3},
            }),
            encoding="utf-8",
        )
        s = load_settings(
            config_path=cfg,
            environ={
                "PYTHON_COMPUTE_HOST": "127.0.0.1",
                "PYTHON_COMPUTE_PORT": "9000",
                "PYTHON_COMPUTE_DEFAULT_TIMEOUT_SEC": "25",
                "PYTHON_COMPUTE_WORKERS": "4",
            },
        )
        assert s.port == 9000
        assert s.default_timeout_sec == 25
        assert s.workers == 4

    def test_workers_default(self) -> None:
        s = load_settings(environ={})
        assert s.workers == 2
        assert s.threads == 2  # workers + ocr_workers, vision off
        direct = ComputeSettings()
        assert direct.workers == 2
        assert direct.threads == 2

    def test_workers_env_and_cli(self) -> None:
        s = load_settings(environ={"PYTHON_COMPUTE_WORKERS": "5", "PYTHON_COMPUTE_HOST": "127.0.0.1"})
        assert s.workers == 5
        assert s.threads == 5
        s2 = load_settings(workers=1, environ={"PYTHON_COMPUTE_WORKERS": "5", "PYTHON_COMPUTE_HOST": "127.0.0.1"})
        assert s2.workers == 1
        assert s2.threads == 1
        both = load_settings(environ={"PYTHON_COMPUTE_WORKERS": "4", "PYTHON_COMPUTE_OCR_WORKERS": "2", "PYTHON_COMPUTE_HOST": "127.0.0.1"})
        assert both.threads == 6

    def test_thread_count_keys_are_rejected(self, tmp_path) -> None:
        # Environment variables we do not read cannot change the listener count.
        ignored = load_settings(environ={"PYTHON_COMPUTE_THREADS": "9", "PYTHON_COMPUTE_MAX_THREADS": "8", "PYTHON_COMPUTE_HOST": "127.0.0.1"})
        assert ignored.threads == 2
        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps({"limits": {"threads": 24, "max_threads": 12, "workers": 4}}), encoding="utf-8")
        with pytest.raises(ConfigError, match="threads"):
            load_settings(config_path=cfg, environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})
        # Top-level threads must be rejected with ConfigError, not crash with TypeError (#1365).
        top_cfg = tmp_path / "top_threads.json"
        top_cfg.write_text(json.dumps({"threads": 16}), encoding="utf-8")
        with pytest.raises(ConfigError, match="threads"):
            load_settings(config_path=top_cfg, environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})

    def test_ocr_timeout_exceeding_max_timeout_rejected(self) -> None:
        """ocr_timeout_sec cannot exceed max_timeout_sec (#1365)."""
        with pytest.raises(ConfigError, match="ocr_timeout_sec cannot exceed max_timeout_sec"):
            ComputeSettings(ocr_timeout_sec=700, max_timeout_sec=600)
        with pytest.raises(ConfigError, match="ocr_timeout_sec cannot exceed max_timeout_sec"):
            # When max_timeout_sec is lower than default ocr_timeout_sec (60)
            ComputeSettings(max_timeout_sec=30)
        with pytest.raises(ConfigError, match="ocr_timeout_sec cannot exceed max_timeout_sec"):
            load_settings(
                environ={
                    "PYTHON_COMPUTE_HOST": "127.0.0.1",
                    "PYTHON_COMPUTE_MAX_TIMEOUT_SEC": "30",
                }
            )
        s = ComputeSettings(ocr_timeout_sec=20, max_timeout_sec=30)
        assert s.ocr_timeout_sec == 20
        assert s.max_timeout_sec == 30

    def test_inflight_keys_are_rejected(self, tmp_path) -> None:
        ignored = load_settings(environ={"PYTHON_COMPUTE_MAX_INFLIGHT": "9", "PYTHON_COMPUTE_MAX_INFLIGHT_PER_SESSION": "3", "PYTHON_COMPUTE_HOST": "127.0.0.1"})
        assert ignored.workers == 2
        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps({"limits": {"max_inflight": 8, "max_inflight_per_session": 4, "workers": 3}}), encoding="utf-8")
        with pytest.raises(ConfigError, match="max_inflight"):
            load_settings(config_path=cfg, environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})

    def test_workers_invalid(self) -> None:
        from compute_service.config import ConfigError

        with pytest.raises(ConfigError):
            load_settings(workers=0, environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})

    @pytest.mark.parametrize(
        "value, value_2, value_3, match",
        [
            # Negative shared_kernel_ttl_sec must be rejected by validate().
            pytest.param("neg_ttl.json", "shared_kernel_ttl_sec", 1, "shared_kernel_ttl_sec", id="test_negative_shared_kernel_ttl_rejected"),
            # Negative idle_worker_ttl_sec must be rejected by validate().
            pytest.param("neg_idle_ttl.json", "idle_worker_ttl_sec", 5, "idle_worker_ttl_sec", id="test_negative_idle_worker_ttl_rejected"),
        ],
    )
    def test_negative_shared_kernel_ttl_rejected(self, tmp_path, value, value_2, value_3, match) -> None:
        from compute_service.config import ConfigError

        cfg = tmp_path / value
        cfg.write_text(json.dumps({"limits": {value_2: -value_3}}), encoding="utf-8")
        with pytest.raises(ConfigError, match=match):
            load_settings(config_path=cfg, environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})


    def test_env_api_key_keeps_surrounding_spaces(self, tmp_path) -> None:
        """The env secret used to be strip()'d. The key file is not, so the same text differed."""
        key = " secret "
        s = load_settings(environ={"PYTHON_COMPUTE_API_KEY": key, "PYTHON_COMPUTE_HOST": "127.0.0.1"})
        assert s.api_key == key
        key_path = tmp_path / "key_spaces"
        key_path.write_bytes(b" secret ")
        from_file = load_settings(api_key_file=key_path, environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})
        assert from_file.api_key == s.api_key

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

    def test_nonfinite_and_huge_numbers_are_config_errors(self, tmp_path) -> None:
        """Infinity / 1e9999 and an oversized JSON integer must be ConfigError.

        json.loads yields inf for those port values, and int(inf) raises
        OverflowError. A JSON integer with no decimal point stays a Python
        int, and float() of one past the float range also raises OverflowError.
        Neither is a ValueError, so they used to kill startup.
        """
        for name, body in (
            ("inf.json", '{"port": Infinity}'),
            ("huge-exp.json", '{"port": 1e9999}'),
        ):
            cfg = tmp_path / name
            cfg.write_text(body, encoding="utf-8")
            with pytest.raises(ConfigError, match="Invalid integer for port") as exc_info:
                load_settings(config_path=cfg, environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})
            assert isinstance(exc_info.value.__cause__, OverflowError)

        ttl = tmp_path / "ttl.json"
        ttl.write_text(
            '{"limits": {"shared_kernel_ttl_sec": ' + ("1" + "0" * 400) + "}}",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="shared_kernel_ttl_sec must be a number") as exc_info:
            load_settings(config_path=ttl, environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})
        assert isinstance(exc_info.value.__cause__, OverflowError)

    @pytest.mark.parametrize(
        ("field", "env_name"),
        [
            ("shared_kernel_ttl_sec", "PYTHON_COMPUTE_SHARED_KERNEL_TTL_SEC"),
            ("idle_worker_ttl_sec", "PYTHON_COMPUTE_IDLE_WORKER_TTL_SEC"),
        ],
    )
    @pytest.mark.parametrize("token", ["Infinity", "NaN", "1e9999"])
    def test_nonfinite_ttl_rejected_from_json_and_env(self, tmp_path, field: str, env_name: str, token: str) -> None:
        """Infinity, NaN, and 1e9999 must not land in a TTL.

        json.loads and float() turn those into inf/nan. inf < 0 and nan < 0
        are false, so the old >= 0 check let them through and the reaper
        never evicted.
        """
        cfg = tmp_path / "ttl.json"
        cfg.write_text(f'{{"limits": {{"{field}": {token}}}}}', encoding="utf-8")
        with pytest.raises(ConfigError, match=rf"{field} must be a finite number"):
            load_settings(config_path=cfg, environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})
        with pytest.raises(ConfigError, match=rf"{field} must be a finite number"):
            load_settings(environ={"PYTHON_COMPUTE_HOST": "127.0.0.1", env_name: token})

    @pytest.mark.parametrize("field", ["shared_kernel_ttl_sec", "idle_worker_ttl_sec"])
    @pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
    def test_compute_settings_rejects_nonfinite_ttl(self, field: str, value: float) -> None:
        """Direct construction skips _as_float. validate() still rejects non-finite TTLs."""
        with pytest.raises(ConfigError, match=rf"{field} must be a finite number"):
            kwargs: dict[str, Any] = {field: value}
            ComputeSettings(**kwargs)

    def test_non_utf8_key_and_config_files_are_config_errors(self, tmp_path) -> None:
        """Binary key and config files raise ConfigError, not UnicodeDecodeError."""
        key_path = tmp_path / "key.bin"
        key_path.write_bytes(b"\xff\xfe")
        with pytest.raises(ConfigError, match="api_key_file") as exc_info:
            load_settings(api_key_file=key_path, environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})
        assert isinstance(exc_info.value.__cause__, UnicodeDecodeError)

        cfg = tmp_path / "cfg.bin"
        cfg.write_bytes(b"\xff\xfe")
        with pytest.raises(ConfigError, match="Cannot read config file") as exc_info:
            load_settings(config_path=cfg, environ={})
        assert isinstance(exc_info.value.__cause__, UnicodeDecodeError)

    def test_compute_settings_none_worker_counts(self) -> None:
        """Verify ComputeSettings handles None for workers and ocr_workers gracefully."""
        kwargs: dict[str, Any] = {"workers": None, "ocr_workers": None}
        s = ComputeSettings(**kwargs)
        assert s.workers == 2
        assert s.ocr_workers == 0
        assert s.threads == 2

    def test_dual_stack_port_zero_bind(self) -> None:
        """Verify WSGIDualStackServer binds cleanly when port=0 and assigns matching port."""
        from compute_service.server import WSGIDualStackServer

        server = WSGIDualStackServer("127.0.0.1", 0)
        try:
            assert len(server.srv.sockets) >= 1
            port = server.srv.server_address[1]
            assert port > 0
            for sock in server.srv.sockets:
                assert sock.getsockname()[1] == port
        finally:
            server.server_close()


@pytest.fixture(scope="class")
def auth_server():
    """One HTTP server for the class; clear `executed` between tests via autouse below."""
    port = get_free_port()
    from compute_service.server import WSGIDualStackServer

    executed: list[str] = []

    def fake_execute(**kwargs):
        executed.append(kwargs["code"])
        return {"status": "ok", "result": 1, "stdout": ""}

    reset_calls: list[str] = []

    def fake_reset(session_id: str, **_kw):
        reset_calls.append(session_id)
        return {"status": "ok"}

    settings = ComputeSettings(host="127.0.0.1", port=port, api_key="correct-secret")
    app = create_wsgi_app(settings, execute_fn=fake_execute, reset_fn=fake_reset)
    server = WSGIDualStackServer("127.0.0.1", port)
    server.set_app(app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.15)
    yield f"http://127.0.0.1:{port}", executed, reset_calls
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


class TestBearerAuthHttp:

    @pytest.fixture(autouse=True)
    def _clear_executed(self, auth_server):
        _url, executed, reset_calls = auth_server
        executed.clear()
        reset_calls.clear()
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
        url, executed, reset_calls = auth_server
        with urllib.request.urlopen(f"{url}/health") as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["status"] == "healthy"
            assert data["service"] == "python-compute"
            assert data["version"] == EXTENSION_VERSION
        assert executed == []
        assert reset_calls == []

    def test_correct_bearer(self, auth_server) -> None:
        url, executed, _reset_calls = auth_server
        status, body = self._post(url, {"Authorization": "Bearer correct-secret"})
        assert status == 200
        assert body["result"] == 1
        assert executed == ["result = 1"]

    def test_missing_bearer(self, auth_server) -> None:
        url, executed, _reset_calls = auth_server
        status, body = self._post(url)
        assert status == 401
        assert body.get("status") == "error"
        assert executed == []

    def test_wrong_bearer(self, auth_server) -> None:
        url, executed, _reset_calls = auth_server
        status, body = self._post(url, {"Authorization": "Bearer wrong"})
        assert status == 401
        assert executed == []

    def test_malformed_bearer(self, auth_server) -> None:
        url, executed, _reset_calls = auth_server
        status, _body = self._post(url, {"Authorization": "Token correct-secret"})
        assert status == 401
        assert executed == []

    def test_bearer_case_insensitive(self, auth_server) -> None:
        url, executed, _reset_calls = auth_server
        status_lower, body_lower = self._post(url, {"Authorization": "bearer correct-secret"})
        assert status_lower == 200
        assert body_lower.get("status") == "ok"
        status_upper, body_upper = self._post(url, {"Authorization": "BEARER correct-secret"})
        assert status_upper == 200
        assert body_upper.get("status") == "ok"
        assert len(executed) == 2

    def test_www_authenticate_header(self, auth_server) -> None:
        url, _executed, _reset_calls = auth_server
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
        url, executed, _reset_calls = auth_server
        # shorter, longer, empty — all must be rejected
        for bad_token in ["x", "correct-secret-plus-extra", ""]:
            status, body = self._post(url, {"Authorization": f"Bearer {bad_token}"})
            assert status == 401, f"Expected 401 for token {bad_token!r}, got {status}"
            assert body.get("status") == "error"
        assert executed == []

    def _post_reset(self, url: str, headers: dict[str, str] | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"{url}/v1/session/reset?session_id=auth-reset-1",
            data=b"{}",
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8")
            return err.code, json.loads(body) if body else {}

    def test_reset_correct_bearer(self, auth_server) -> None:
        url, executed, reset_calls = auth_server
        status, body = self._post_reset(url, {"Authorization": "Bearer correct-secret"})
        assert status == 200
        assert body.get("status") == "ok"
        assert reset_calls == ["auth-reset-1"]
        assert executed == []

    def test_reset_missing_bearer(self, auth_server) -> None:
        url, executed, reset_calls = auth_server
        status, body = self._post_reset(url)
        assert status == 401
        assert body.get("status") == "error"
        assert reset_calls == []
        assert executed == []

    def test_reset_wrong_bearer(self, auth_server) -> None:
        url, executed, reset_calls = auth_server
        status, body = self._post_reset(url, {"Authorization": "Bearer wrong"})
        assert status == 401
        assert reset_calls == []
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
        assert parsed["code"] == "INTERNAL_ERROR"
        assert parsed.get("id") == "v-x"
        assert "vision boom" not in parsed["error"]

    def test_execute_unhandled_exception_is_json_500(self) -> None:
        def boom_execute(*args, **kwargs):
            raise RuntimeError("execute boom")

        app = create_wsgi_app(ComputeSettings(), execute_fn=boom_execute)
        payload = json.dumps({"id": "ex-x", "code": "1+1"}).encode("utf-8")
        status, headers, body = _wsgi_post(app, payload, path="/v1/execute", headers={"Host": "127.0.0.1"})

        assert status.startswith("500")
        assert body["status"] == "error"
        assert body["code"] == "INTERNAL_ERROR"
        assert "execute boom" not in body["error"]
        assert body["id"] == "ex-x"

    def test_reset_unhandled_exception_is_json_500(self) -> None:
        def boom_reset(*args, **kwargs):
            raise RuntimeError("reset boom")

        app = create_wsgi_app(ComputeSettings(), reset_fn=boom_reset)
        payload = json.dumps({"id": "ex-x"}).encode("utf-8")
        status, headers, body = _wsgi_post(app, payload, path="/v1/session/reset", query="session_id=1", headers={"Host": "127.0.0.1"})

        assert status.startswith("500")
        assert body["status"] == "error"
        assert body["code"] == "INTERNAL_ERROR"
        assert "reset boom" not in body["error"]
        assert body["id"] == "ex-x"


class TestSessionResetHttp:
    def test_missing_session_id_is_400(self) -> None:
        app = create_wsgi_app(ComputeSettings(), reset_fn=lambda sid, **_kw: {"status": "ok"})
        status, _headers, body = _wsgi_post(app, b"{}", query="")
        assert status.startswith("400")
        assert body.get("status") == "error"
        assert "session_id" in body.get("error", "")

    def test_empty_session_id_is_400(self) -> None:
        app = create_wsgi_app(ComputeSettings(), reset_fn=lambda sid, **_kw: {"status": "ok"})
        status, _headers, body = _wsgi_post(app, b'{"id": "empty-sid"}', query="session_id=")
        assert status.startswith("400")
        assert body.get("id") == "empty-sid"
        assert body.get("status") == "error"

    def test_whitespace_session_id_is_400(self) -> None:
        app = create_wsgi_app(ComputeSettings(), reset_fn=lambda sid, **_kw: {"status": "ok"})
        status, _headers, body = _wsgi_post(app, b"{}", query="session_id=%20")
        assert status.startswith("400")
        assert body.get("status") == "error"

    def test_session_id_in_body_is_400(self) -> None:
        reset_calls: list[str] = []

        def fake_reset(session_id: str, **_kw):
            reset_calls.append(session_id)
            return {"status": "ok"}

        app = create_wsgi_app(ComputeSettings(), reset_fn=fake_reset)
        status, _headers, body = _wsgi_post(
            app,
            json.dumps({"id": "body-sid", "session_id": "nope"}).encode("utf-8"),
            query="session_id=ok-query",
        )
        assert status.startswith("400")
        assert body.get("id") == "body-sid"
        assert "query parameter" in body.get("error", "")
        assert reset_calls == []

    def test_unknown_session_is_200_ok(self) -> None:
        seen: list[str] = []

        def fake_reset(session_id: str, **_kw):
            seen.append(session_id)
            return {"status": "ok"}

        app = create_wsgi_app(ComputeSettings(), reset_fn=fake_reset)
        status, _headers, body = _wsgi_post(
            app,
            json.dumps({"id": "unk-1"}).encode("utf-8"),
            query="session_id=never-seen",
        )
        assert status.startswith("200")
        assert body == {"id": "unk-1", "status": "ok"}
        assert seen == ["never-seen"]

    @pytest.mark.parametrize(
        "value, query",
        [
            pytest.param(b"", "session_id=empty-body", id="test_empty_body_is_ok"),
            pytest.param(None, "session_id=no-cl", id="test_missing_content_length_is_ok"),
        ],
    )
    def test_empty_body_is_ok(self, value, query) -> None:
        app = create_wsgi_app(
            ComputeSettings(),
            reset_fn=lambda sid, **_kw: {"status": "ok"},
        )
        status, _headers, body = _wsgi_post(app, value, query=query)
        assert status.startswith("200")
        assert body == {"status": "ok"}


    def test_chunked_transfer_encoding_is_400(self) -> None:
        """Chunked reset used to succeed as an empty body and drop the payload."""
        reset_calls: list[str] = []

        def fake_reset(session_id: str, **_kw):
            reset_calls.append(session_id)
            return {"status": "ok"}

        app = create_wsgi_app(ComputeSettings(), reset_fn=fake_reset, execute_fn=lambda **_kw: {"status": "ok"})
        status, _headers, body = _wsgi_post(
            app,
            b"{}",
            query="session_id=chunked-sid",
            headers={"Transfer-Encoding": "chunked", "Content-Type": "application/json"},
        )
        assert status.startswith("400")
        assert "chunked" in body.get("error", "").lower()
        assert reset_calls == []

        exec_status, _exec_headers, exec_body = _wsgi_post(
            app,
            json.dumps({"code": "result = 1"}).encode("utf-8"),
            path="/v1/execute",
            headers={"Transfer-Encoding": "chunked", "Content-Type": "application/json"},
        )
        assert exec_status.startswith("400")
        assert "chunked" in exec_body.get("error", "").lower()

    def test_lease_failure_is_503(self) -> None:
        def busy_reset(_session_id: str, **_kw):
            return {
                "status": "error",
                "code": "WORKER_POOL_BUSY",
                "error": "Could not lease worker to reset session.",
            }

        app = create_wsgi_app(ComputeSettings(), reset_fn=busy_reset)
        status, _headers, body = _wsgi_post(
            app,
            json.dumps({"id": "busy-1"}).encode("utf-8"),
            query="session_id=busy-sid",
        )
        assert status.startswith("503")
        assert body.get("id") == "busy-1"
        assert body.get("status") == "error"
        assert body.get("code") == "WORKER_POOL_BUSY"

    def test_execute_pool_busy_is_503(self) -> None:
        def busy(**_kwargs):
            return {"id": "ex-busy", "status": "error", "code": "WORKER_POOL_BUSY", "error": "busy"}

        app = create_wsgi_app(ComputeSettings(), execute_fn=busy)
        status, _headers, body = _wsgi_post(app, json.dumps({"code": "result = 1"}).encode("utf-8"), path="/v1/execute")
        assert status.startswith("503")
        assert body.get("code") == "WORKER_POOL_BUSY"

    def test_execute_worker_death_is_500(self) -> None:
        """A crash or empty frame may have run the cell, so it is not a retryable 503."""
        def dead(**_kwargs):
            return {"status": "error", "code": "WORKER_CRASHED", "error": "died"}

        app = create_wsgi_app(ComputeSettings(), execute_fn=dead)
        status, _headers, body = _wsgi_post(app, json.dumps({"id": "ex-dead", "code": "result = 1"}).encode("utf-8"), path="/v1/execute")
        assert status.startswith("500")
        assert body.get("code") == "WORKER_CRASHED"

    def test_execute_empty_response_is_500(self) -> None:
        def empty(**_kwargs):
            return {"status": "error", "code": "EMPTY_RESPONSE", "error": "no frame"}

        app = create_wsgi_app(ComputeSettings(), execute_fn=empty)
        status, _headers, body = _wsgi_post(app, json.dumps({"code": "result = 1"}).encode("utf-8"), path="/v1/execute")
        assert status.startswith("500")
        assert body.get("code") == "EMPTY_RESPONSE"

    def test_execute_timeout_stays_200(self) -> None:
        def timed_out(**_kwargs):
            return {"status": "error", "code": "EXECUTION_TIMEOUT", "error": "too slow"}

        app = create_wsgi_app(ComputeSettings(), execute_fn=timed_out)
        status, _headers, body = _wsgi_post(app, json.dumps({"code": "result = 1"}).encode("utf-8"), path="/v1/execute")
        assert status.startswith("200")
        assert body.get("code") == "EXECUTION_TIMEOUT"

    def test_vision_pool_busy_is_503(self) -> None:
        """A vision lease miss used to be HTTP 200. The accept-deadline pre-check is already 503."""
        fake_pool = MagicMock()
        fake_pool.execute.return_value = {
            "id": "v-busy",
            "status": "error",
            "code": "VISION_POOL_BUSY",
            "error": "All vision workers are currently busy and request timed out waiting for worker lease.",
        }
        app = create_wsgi_app(ComputeSettings())
        payload = json.dumps({"id": "v-busy", "image_b64": "YQ=="}).encode("utf-8")
        with patch("compute_service.vision_pool.get_vision_pool", return_value=fake_pool):
            status, _headers, body = _wsgi_post(app, payload, path="/v1/vision")
        assert status.startswith("503")
        assert body.get("code") == "VISION_POOL_BUSY"
        assert body.get("id") == "v-busy"
        assert body.get("status") == "error"

    def test_vision_file_errors_use_client_statuses(self) -> None:
        """Allowlist file failures used to leave the route as HTTP 200."""
        cases = (
            ("FILE_NOT_FOUND", "400"),
            ("FILE_TOO_LARGE", "413"),
        )
        for code, expect in cases:
            fake_pool = MagicMock()
            fake_pool.execute.return_value = {
                "id": "v-file",
                "status": "error",
                "code": code,
                "error": code,
            }
            app = create_wsgi_app(ComputeSettings())
            payload = json.dumps({"id": "v-file", "image_b64": "YQ=="}).encode("utf-8")
            with patch("compute_service.vision_pool.get_vision_pool", return_value=fake_pool):
                status, _headers, body = _wsgi_post(app, payload, path="/v1/vision")
            assert status.startswith(expect), code
            assert body.get("code") == code

    def test_vision_unavailable_is_503(self) -> None:
        """VISION_UNAVAILABLE happens when the OCR module fails to import, must be 503."""
        fake_pool = MagicMock()
        fake_pool.execute.return_value = {
            "id": "v-missing",
            "status": "error",
            "code": "VISION_UNAVAILABLE",
            "error": "OCR is not installed in this server",
        }
        app = create_wsgi_app(ComputeSettings())
        payload = json.dumps({"id": "v-missing", "image_b64": "YQ=="}).encode("utf-8")
        with patch("compute_service.vision_pool.get_vision_pool", return_value=fake_pool):
            status, _headers, body = _wsgi_post(app, payload, path="/v1/vision")
        assert status.startswith("503")
        assert body.get("code") == "VISION_UNAVAILABLE"
        assert body.get("id") == "v-missing"

    def test_formula_permits_do_not_starve_vision_admission(self) -> None:
        """workers=2 and ocr_workers=4 used to share six permits.

        Six in-flight executes then held every permit, so /v1/vision returned
        VISION_POOL_BUSY while OCR workers were still idle.
        """
        settings = ComputeSettings(workers=2, ocr_workers=4)
        hold = threading.Event()
        entered_lock = threading.Lock()
        entered_count = 0
        both_in = threading.Event()

        def execute_fn(**_kwargs: Any) -> dict[str, Any]:
            nonlocal entered_count
            with entered_lock:
                entered_count += 1
                if entered_count >= 2:
                    both_in.set()
            assert hold.wait(timeout=5)
            return {"status": "ok", "result_json": b'{"status":"ok","result":1}'}

        app = create_wsgi_app(settings, execute_fn=execute_fn)
        body = json.dumps({"code": "result = 1"}).encode("utf-8")
        results: list[tuple[str, str | None]] = []
        results_lock = threading.Lock()

        def post() -> None:
            status, _headers, parsed = _wsgi_post(app, body, path="/v1/execute")
            code = parsed.get("code") if isinstance(parsed, dict) else None
            with results_lock:
                results.append((status, code))

        threads = [threading.Thread(target=post) for _ in range(6)]
        fake_pool = MagicMock()
        fake_pool.execute.return_value = {"id": "v-free", "status": "ok", "text": "ok"}
        payload = json.dumps({"id": "v-free", "image_b64": "YQ=="}).encode("utf-8")
        try:
            for thread in threads:
                thread.start()
            assert both_in.wait(timeout=5)
            deadline = time.monotonic() + 2.0
            busy: list[tuple[str, str | None]] = []
            while time.monotonic() < deadline:
                with results_lock:
                    busy = [item for item in results if item[0].startswith("503")]
                if len(busy) >= 4:
                    break
                time.sleep(0.01)
            assert busy == [("503 Service Unavailable", "WORKER_POOL_BUSY")] * 4
            assert entered_count == 2
            with patch("compute_service.vision_pool.get_vision_pool", return_value=fake_pool):
                vstatus, _vheaders, vbody = _wsgi_post(app, payload, path="/v1/vision")
            assert vstatus.startswith("200")
            assert vbody.get("code") != "VISION_POOL_BUSY"
            fake_pool.execute.assert_called_once()
        finally:
            hold.set()
            for thread in threads:
                thread.join(timeout=5)

    def test_vision_permits_do_not_starve_formula_admission(self) -> None:
        """The reverse of the shared-permit bug: a full vision pool must not 503 execute."""
        settings = ComputeSettings(workers=2, ocr_workers=4)
        hold = threading.Event()
        entered_lock = threading.Lock()
        entered_count = 0
        all_in = threading.Event()

        def vision_execute(**_kwargs: Any) -> dict[str, Any]:
            nonlocal entered_count
            with entered_lock:
                entered_count += 1
                if entered_count >= 4:
                    all_in.set()
            assert hold.wait(timeout=5)
            return {"status": "ok", "text": "ok"}

        fake_pool = MagicMock()
        fake_pool.execute.side_effect = vision_execute

        def execute_fn(**_kwargs: Any) -> dict[str, Any]:
            return {"status": "ok", "result_json": b'{"status":"ok","result":1}'}

        app = create_wsgi_app(settings, execute_fn=execute_fn)
        payload = json.dumps({"image_b64": "YQ=="}).encode("utf-8")
        results: list[tuple[str, str | None]] = []
        results_lock = threading.Lock()

        def post() -> None:
            status, _headers, parsed = _wsgi_post(app, payload, path="/v1/vision")
            code = parsed.get("code") if isinstance(parsed, dict) else None
            with results_lock:
                results.append((status, code))

        threads = [threading.Thread(target=post) for _ in range(5)]
        try:
            with patch("compute_service.vision_pool.get_vision_pool", return_value=fake_pool):
                for thread in threads:
                    thread.start()
                assert all_in.wait(timeout=5)
                deadline = time.monotonic() + 2.0
                busy: list[tuple[str, str | None]] = []
                while time.monotonic() < deadline:
                    with results_lock:
                        busy = [item for item in results if item[0].startswith("503")]
                    if len(busy) >= 1:
                        break
                    time.sleep(0.01)
                assert busy == [("503 Service Unavailable", "VISION_POOL_BUSY")]
                assert entered_count == 4
                estatus, _eheaders, ebody = _wsgi_post(
                    app,
                    json.dumps({"code": "result = 1"}).encode("utf-8"),
                    path="/v1/execute",
                )
            assert estatus.startswith("200")
            assert ebody.get("code") != "WORKER_POOL_BUSY"
        finally:
            hold.set()
            for thread in threads:
                thread.join(timeout=5)

    def test_non_ascii_bearer_and_key_file(self, tmp_path) -> None:
        """hmac.compare_digest on str raises TypeError for non-ASCII. That escaped the WSGI app."""
        from compute_service.server import authenticate_request

        key = "sécret-ключ"
        key_path = tmp_path / "key"
        key_path.write_text(key + "\n", encoding="utf-8")
        settings = load_settings(api_key_file=key_path, environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})
        assert settings.api_key == key

        principal, err = authenticate_request({"HTTP_AUTHORIZATION": f"Bearer {key}"}, settings)
        assert err is None
        assert principal == settings.default_principal
        principal_bad, err_bad = authenticate_request({"HTTP_AUTHORIZATION": "Bearer café-nope"}, settings)
        assert principal_bad is None
        assert err_bad == "invalid"

        ran: list[str] = []

        def execute_fn(**kwargs):
            ran.append(kwargs["code"])
            return {"status": "ok", "result": 1, "stdout": ""}

        app = create_wsgi_app(settings, execute_fn=execute_fn)
        bad_status, bad_headers, bad_body = _wsgi_post(
            app,
            json.dumps({"code": "result = 1"}).encode("utf-8"),
            path="/v1/execute",
            headers={"Authorization": "Bearer café-nope"},
        )
        assert bad_status.startswith("401")
        assert bad_body.get("error") == "Unauthorized"
        assert ("WWW-Authenticate", "Bearer") in bad_headers
        assert ran == []

        ok_status, _ok_headers, ok_body = _wsgi_post(
            app,
            json.dumps({"code": "result = 1"}).encode("utf-8"),
            path="/v1/execute",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert ok_status.startswith("200")
        assert ok_body.get("status") == "ok"
        assert ran == ["result = 1"]

    def test_early_error_drains_body_401_and_413(self) -> None:
        """401 and 413 must drain wsgi.input (bounded for 413) so the connection is not broken."""
        key = "drain-secret-key"
        settings = ComputeSettings(
            api_key=key,
            max_body_bytes=1024,
        )
        app = create_wsgi_app(settings)

        # 1. 401 Unauthorized drains the body
        body_401 = b"some request body for 401"
        stream_401 = io.BytesIO(body_401)
        status_holder: list[str] = []

        def start_response_401(status: str, headers: list) -> None:
            status_holder.append(status)

        environ_401 = {
            "PATH_INFO": "/v1/execute",
            "REQUEST_METHOD": "POST",
            "QUERY_STRING": "",
            "CONTENT_LENGTH": str(len(body_401)),
            "wsgi.input": stream_401,
            "HTTP_AUTHORIZATION": "Bearer wrong-key",
        }
        res_iter = app(environ_401, start_response_401)
        _ = b"".join(res_iter)
        assert status_holder[0].startswith("401")
        assert stream_401.tell() == len(body_401)

        # 2. 413 Payload Too Large drains the body bounded to 64 KiB
        large_len = 100 * 1024  # 100 KiB > max_body_bytes (1024)
        stream_413 = io.BytesIO(b"x" * large_len)
        status_holder.clear()

        def start_response_413(status: str, headers: list) -> None:
            status_holder.append(status)

        environ_413 = {
            "PATH_INFO": "/v1/execute",
            "REQUEST_METHOD": "POST",
            "QUERY_STRING": "",
            "CONTENT_LENGTH": str(large_len),
            "wsgi.input": stream_413,
            "HTTP_AUTHORIZATION": f"Bearer {key}",
        }
        res_iter = app(environ_413, start_response_413)
        _ = b"".join(res_iter)
        assert status_holder[0].startswith("413")
        # Bounded drain: should have read exactly 64 KiB (65536 bytes), NOT all 100 KiB
        assert stream_413.tell() == 64 * 1024

    def test_execute_queue_timeout_from_pool_is_503(self) -> None:
        """The pool's own deadline check must not answer 200.

        The handler returns 503 when the accept deadline is already gone.
        If it expires in the gap before the pool checks, the pool returns
        QUEUE_TIMEOUT with no result_json. That used to fall through to 200.
        """

        def late(**_kwargs):
            return {"status": "error", "code": "QUEUE_TIMEOUT", "error": "Request deadline expired before a worker lease."}

        app = create_wsgi_app(ComputeSettings(), execute_fn=late)
        status, _headers, body = _wsgi_post(
            app,
            json.dumps({"id": "q-late", "code": "result = 1"}).encode("utf-8"),
            path="/v1/execute",
        )
        assert status.startswith("503")
        assert body.get("id") == "q-late"
        assert body.get("code") == "QUEUE_TIMEOUT"
        assert body.get("status") == "error"

    def test_overflow_id_is_400(self) -> None:
        def execute_fn(**_kwargs):
            raise AssertionError("non-finite id must not run")

        app = create_wsgi_app(ComputeSettings(), execute_fn=execute_fn)
        status, _headers, body = _wsgi_post(app, b'{"id":1e9999,"code":"result = 1"}', path="/v1/execute")
        assert status.startswith("400")
        assert body.get("error") == "Invalid JSON"

    @pytest.mark.parametrize("raw_id", [b"1e9999", b"NaN", b"Infinity"])
    def test_vision_nonfinite_id_is_400(self, raw_id: bytes) -> None:
        """A non-finite vision id used to crash the response, then the 500 fallback."""
        from compute_service.vision_pool import shutdown_vision_pool

        app = create_wsgi_app(ComputeSettings())
        try:
            status, _headers, body = _wsgi_post(
                app,
                b'{"id":' + raw_id + b',"image_b64":"abcd"}',
                path="/v1/vision",
            )
            assert status.startswith("400")
            assert body.get("error") == "Invalid JSON"
            assert "id" not in body
        finally:
            shutdown_vision_pool()

    def test_vision_overflow_timeout_is_json(self) -> None:
        from compute_service.vision_pool import shutdown_vision_pool

        app = create_wsgi_app(ComputeSettings())
        try:
            status, _headers, body = _wsgi_post(
                app,
                b'{"image_b64":"abcd","timeout_ms":1e9999}',
                path="/v1/vision",
            )
            assert status.startswith("501")
            assert body.get("code") == "VISION_SERVICE_DISABLED"
        finally:
            shutdown_vision_pool()

    def test_execute_shutdown_is_503(self) -> None:
        def down(**_kwargs):
            return {"status": "error", "code": "SERVICE_SHUTDOWN", "error": "stopping"}

        app = create_wsgi_app(ComputeSettings(), execute_fn=down)
        status, _headers, body = _wsgi_post(app, json.dumps({"id": "ex-down", "code": "result = 1"}).encode("utf-8"), path="/v1/execute")
        assert status.startswith("503")
        assert body.get("id") == "ex-down"
        assert body.get("code") == "SERVICE_SHUTDOWN"

    def test_execute_eval_error_stays_200(self) -> None:
        def failed(**_kwargs):
            return {"status": "error", "result_json": b'{"status":"error","error":"boom"}'}

        app = create_wsgi_app(ComputeSettings(), execute_fn=failed)
        status, _headers, body = _wsgi_post(app, json.dumps({"code": "result = 1"}).encode("utf-8"), path="/v1/execute")
        assert status.startswith("200")
        assert body.get("error") == "boom"

    def test_execute_worker_fault_inside_result_json_stays_200(self) -> None:
        """Formula wraps WORKER_EXECUTION_ERROR in result_json. Those bytes stay 200.

        Re-statusing them to 500 would hide the cell text behind #N/A. The
        dict-shaped code (no result_json) is the 500 path.
        """
        raw = b'{"status":"error","code":"WORKER_EXECUTION_ERROR","error":"boom","stdout":""}'

        def failed(**_kwargs):
            return {"status": "error", "code": "WORKER_EXECUTION_ERROR", "result_json": raw}

        app = create_wsgi_app(ComputeSettings(), execute_fn=failed)
        status, _headers, body = _wsgi_post(app, json.dumps({"code": "result = 1"}).encode("utf-8"), path="/v1/execute")
        assert status.startswith("200")
        assert body.get("code") == "WORKER_EXECUTION_ERROR"
        assert body.get("error") == "boom"

    def test_expired_accept_deadline_does_not_run(self) -> None:
        """Queue time counts against the cell timeout. A late request does not run."""

        def execute_fn(**_kwargs):
            raise AssertionError("expired request must not run")

        app = create_wsgi_app(ComputeSettings(), execute_fn=execute_fn)
        body = json.dumps({"id": "late", "code": "result = 1", "timeout_ms": 1000}).encode("utf-8")
        status_holder: list[str] = []

        def start_response(status: str, resp_headers: list) -> None:
            status_holder.append(status)
            del resp_headers

        environ = {
            "PATH_INFO": "/v1/execute",
            "REQUEST_METHOD": "POST",
            "QUERY_STRING": "",
            "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
            "compute.accept_time": time.monotonic() - 5.0,
        }
        out = b"".join(app(environ, start_response))
        parsed = json.loads(out.decode("utf-8"))
        assert status_holder[0].startswith("503")
        assert parsed.get("code") == "QUEUE_TIMEOUT"
        assert parsed.get("id") == "late"
        assert parsed.get("status") == "error"

    def test_unknown_mode_is_400(self) -> None:
        def execute_fn(**_kwargs):
            raise AssertionError("bad mode must not run")

        app = create_wsgi_app(ComputeSettings(), execute_fn=execute_fn)
        status, _headers, body = _wsgi_post(
            app,
            json.dumps({"id": "mode-1", "code": "result = 1", "mode": "Shared"}).encode("utf-8"),
            path="/v1/execute",
        )
        assert status.startswith("400")
        assert body.get("id") == "mode-1"
        assert "mode" in body.get("error", "")

    def test_peel_init_script_over_cap_is_400(self) -> None:
        def execute_fn(**_kwargs):
            raise AssertionError("oversize init must not run")

        app = create_wsgi_app(ComputeSettings(max_code_chars=64), execute_fn=execute_fn)
        status, _headers, body = _wsgi_post(
            app,
            json.dumps({"id": "init-big", "code": "result = 1", "init_script": "i" * 65}).encode("utf-8"),
            path="/v1/execute",
        )
        assert status.startswith("400")
        assert body.get("code") == "CODE_TOO_LARGE"
        assert body.get("id") == "init-big"

    @pytest.mark.parametrize("raw_id", [b"1e9999", b"NaN", b"Infinity", b"-Infinity"])
    def test_nonfinite_id_is_400_and_does_not_reset(self, raw_id: bytes) -> None:
        """json.loads accepts these. Echoing them crashed allow_nan=False after reset ran."""

        def reset_fn(_session_id: str, **_kwargs):
            raise AssertionError("non-finite id must not reset")

        app = create_wsgi_app(ComputeSettings(), reset_fn=reset_fn)
        status, _headers, body = _wsgi_post(app, b'{"id":' + raw_id + b"}", query="session_id=sid")
        assert status.startswith("400")
        assert body.get("error") == "Invalid JSON"
        assert "id" not in body

    def test_auth_required_matches_execute(self) -> None:
        app = create_wsgi_app(
            ComputeSettings(api_key="reset-secret"),
            reset_fn=lambda sid, **_kw: {"status": "ok"},
        )
        status, headers, body = _wsgi_post(app, b"{}", query="session_id=authed")
        assert status.startswith("401")
        assert body.get("status") == "error"
        assert ("WWW-Authenticate", "Bearer") in headers

        status_ok, _headers_ok, body_ok = _wsgi_post(
            app,
            b"{}",
            query="session_id=authed",
            headers={"Authorization": "Bearer reset-secret"},
        )
        assert status_ok.startswith("200")
        assert body_ok.get("status") == "ok"


class TestImportBoundary:
    @pytest.mark.parametrize(
        "value",
        [
            # Config + auth app construction must not import plugin.framework.config
            #         or open writeragent.json (executor sandbox coupling is deferred to first execute).
            pytest.param(r"""
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
""", id="test_config_auth_startup_avoids_writeragent_config"),
            # Master compute service server must not load heavy packages (numpy, sympy) into memory.
            pytest.param(r"""
import sys
from compute_service.config import load_settings
from compute_service.server import create_wsgi_app, WSGIDualStackServer

s = load_settings(environ={"PYTHON_COMPUTE_HOST": "127.0.0.1"})
app = create_wsgi_app(s)
assert "numpy" not in sys.modules, f"numpy was loaded into master process: {sys.modules.get('numpy')}"
assert "sympy" not in sys.modules, f"sympy was loaded into master process: {sys.modules.get('sympy')}"
print("ok")
""", id="test_server_startup_does_not_import_numpy_or_sympy"),
        ],
    )
    def test_config_auth_startup_avoids_writeragent_config(self, value) -> None:
        import subprocess
        import sys
        from pathlib import Path

        repo = Path(__file__).resolve().parents[2]
        code = value
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


def _run_docker_entrypoint(tmp_path, extra: dict[str, str]):
    import subprocess
    import sys
    from pathlib import Path

    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "ran"
    fake = bindir / "python"
    repo_root = Path(__file__).resolve().parents[2]
    fake_script = (
        '#!/bin/sh\n'
        'export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"\n'
        '"$REAL_PYTHON" -c "from compute_service.config import load_settings; s = load_settings(); s.validate()" || exit 1\n'
        'printf \'%s\\n\' "$@" > "$FAKE_OUT"\n'
    )
    fake.write_text(fake_script, encoding="utf-8")
    fake.chmod(0o755)
    # /bin stays on PATH so /bin/sh can run; the fake python is first.
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "FAKE_OUT": str(marker),
        "REAL_PYTHON": sys.executable,
        "REPO_ROOT": str(repo_root),
    }
    env.update(extra)
    script = repo_root / "compute_service" / "docker-entrypoint.sh"
    proc = subprocess.run(["/bin/sh", str(script)], env=env, capture_output=True, text=True, check=False)
    ran = marker.read_text(encoding="utf-8") if marker.exists() else ""
    return proc, ran


@pytest.mark.skipif(sys.platform == "win32", reason="runs a POSIX shell script")
class TestDockerEntrypoint:
    def test_wildcard_without_key_exits(self, tmp_path) -> None:
        proc, ran = _run_docker_entrypoint(tmp_path, {"PYTHON_COMPUTE_HOST": "0.0.0.0"})
        assert proc.returncode == 1
        assert ran == ""
        assert "PYTHON_COMPUTE_API_KEY" in proc.stderr

    def test_ipv6_wildcard_without_key_exits(self, tmp_path) -> None:
        proc, ran = _run_docker_entrypoint(tmp_path, {"PYTHON_COMPUTE_HOST": "::"})
        assert proc.returncode == 1
        assert ran == ""

    def test_wildcard_with_key_starts_server(self, tmp_path) -> None:
        proc, ran = _run_docker_entrypoint(tmp_path, {"PYTHON_COMPUTE_HOST": "0.0.0.0", "PYTHON_COMPUTE_API_KEY": "secret"})
        assert proc.returncode == 0
        assert "compute_service/server.py" in ran

    def test_loopback_without_key_starts_server(self, tmp_path) -> None:
        proc, ran = _run_docker_entrypoint(tmp_path, {"PYTHON_COMPUTE_HOST": "127.0.0.1"})
        assert proc.returncode == 0
        assert "compute_service/server.py" in ran


class _AcceptedConnectionHandler:
    """Request handler stand-in that does not treat the socket as a Mock spec.

    ``MagicMock(sock, address, server)`` binds the socket to ``spec``. A
    MagicMock socket then raises InvalidSpecError inside the listener thread.
    """

    def __init__(self, request: object, client_address: object, server: object) -> None:
        self.request = request
        self.client_address = client_address
        self.server = server


class TestListenerQueue:
    def test_busy_listener_pool_queues_instead_of_503(self) -> None:
        """A busy accept pool queues the connection. It does not 503 and close it.

        The executor queue is unbounded, so an extra accept waits for a thread.
        A worker or vision semaphore miss is a different gate: non-blocking,
        HTTP 503, and it does not hold a listener.
        """
        from compute_service.server import DualStackThreadPoolHTTPServer

        server = DualStackThreadPoolHTTPServer(("127.0.0.1", 0), _AcceptedConnectionHandler, max_threads=1)
        hold = threading.Event()
        running = threading.Event()

        def _occupy() -> None:
            running.set()
            hold.wait(timeout=5)

        try:
            server.executor.submit(_occupy)
            assert running.wait(timeout=2)
            # The queue has no cap. One extra task waits instead of a 503.
            server.executor.submit(_occupy)
            deadline = time.monotonic() + 2
            while server.executor._work_queue.qsize() < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.executor._work_queue.qsize() >= 1

            sock = MagicMock()
            before = server.executor._work_queue.qsize()
            server.process_request(sock, ("127.0.0.1", 9))
            sock.sendall.assert_not_called()
            sock.close.assert_not_called()
            assert server.executor._work_queue.qsize() == before + 1
        finally:
            hold.set()
            server.server_close()


def test_flatten_config_json_rejects_api_key() -> None:
    """_flatten_config_json raises ConfigError if api_key is present in config JSON."""
    from compute_service.config import ConfigError, _flatten_config_json

    with pytest.raises(ConfigError, match="Do not put api_key in the JSON config"):
        _flatten_config_json({"api_key": "secret"})

    with pytest.raises(ConfigError, match="Do not put api_key in the JSON config"):
        _flatten_config_json({"auth": {"api_key": "secret"}})


def test_sticky_slots_scale_with_formula_workers() -> None:
    """Four formula workers used to share one sticky listener slot."""
    from compute_service.server import listener_thread_count, service_listener_threads, sticky_listener_slots

    wide = ComputeSettings(workers=4, ocr_workers=0)
    assert sticky_listener_slots(wide) == 4
    pool_threads = service_listener_threads(wide)
    vision_permits = 1
    assert pool_threads >= wide.workers + vision_permits + sticky_listener_slots(wide) + 2

    small = ComputeSettings(workers=2, ocr_workers=0)
    assert service_listener_threads(small) == listener_thread_count(small.threads) == 8
    assert sticky_listener_slots(small) == 3


@pytest.mark.parametrize("code", ["VISION_WORKER_ERROR", "WORKER_EXECUTION_ERROR"])
def test_dict_worker_fault_is_http_500(code: str) -> None:
    """A worker fault with no result_json is 500. It used to fall through to 200."""
    from compute_service.server import _send_execution_result

    status_holder: list[str] = []

    def start_response(status: str, resp_headers: list) -> None:
        status_holder.append(status)
        del resp_headers

    out = _send_execution_result(
        start_response,
        {"status": "error", "code": code, "error": "worker blew up"},
        "fault-1",
    )
    parsed = json.loads(b"".join(out))
    assert status_holder[0].startswith("500")
    assert parsed.get("code") == code
    assert parsed.get("id") == "fault-1"


def test_result_json_worker_fault_bytes_are_forwarded_at_200() -> None:
    """The host must not re-dumps or re-status a forwarded cell body."""
    from compute_service.server import _send_execution_result

    raw = b'{"status":"error","code":"WORKER_EXECUTION_ERROR","error":"boom"}'
    status_holder: list[str] = []

    def start_response(status: str, resp_headers: list) -> None:
        status_holder.append(status)
        del resp_headers

    out = _send_execution_result(
        start_response,
        {"status": "error", "code": "WORKER_EXECUTION_ERROR", "result_json": raw},
        "cell-1",
    )
    assert status_holder[0].startswith("200")
    assert out == [raw]


def test_worker_file_path_denied_is_http_400() -> None:
    """The route returns 400. The worker re-check used to stay HTTP 200."""
    from compute_service.server import _send_execution_result

    status_holder: list[str] = []

    def start_response(status: str, resp_headers: list) -> None:
        status_holder.append(status)
        del resp_headers

    out = _send_execution_result(
        start_response,
        {"status": "error", "code": "FILE_PATH_DENIED", "error": "denied"},
        "path-1",
    )
    parsed = json.loads(b"".join(out))
    assert status_holder[0].startswith("400")
    assert parsed.get("code") == "FILE_PATH_DENIED"
    assert parsed.get("id") == "path-1"


def test_wrong_method_is_405() -> None:
    app = create_wsgi_app(
        ComputeSettings(host="127.0.0.1", port=9, workers=1),
        execute_fn=lambda **_kwargs: {"status": "ok"},
        reset_fn=lambda _sid: {"status": "ok"},
    )

    def call(method: str, path: str) -> tuple[str, list[tuple[str, str]]]:
        status_holder: list[str] = []
        header_holder: list[tuple[str, str]] = []

        def start_response(status: str, resp_headers: list) -> None:
            status_holder.append(status)
            header_holder.extend(resp_headers)

        environ = {
            "PATH_INFO": path,
            "REQUEST_METHOD": method,
            "QUERY_STRING": "",
            "wsgi.input": io.BytesIO(b""),
        }
        app(environ, start_response)
        return status_holder[0], header_holder

    status, headers = call("POST", "/health")
    assert status.startswith("405")
    assert ("Allow", "GET") in headers
    status, headers = call("GET", "/v1/execute")
    assert status.startswith("405")
    assert ("Allow", "POST") in headers
    status, headers = call("GET", "/no-such")
    assert status.startswith("404")


def test_send_execution_result_drops_unencodable_id() -> None:
    """The 500 fallback must still write when req_id itself is not strict JSON."""
    from compute_service.server import _send_execution_result

    status_holder: list[str] = []

    def start_response(status: str, resp_headers: list) -> None:
        status_holder.append(status)
        del resp_headers

    out = _send_execution_result(start_response, {"status": "ok", "result": float("nan")}, float("inf"))
    parsed = json.loads(b"".join(out))
    assert status_holder[0].startswith("500")
    assert "id" not in parsed
    assert "JSON encode failed" in parsed.get("error", "")


def test_partial_bind_address_in_use_raises() -> None:
    """One family in use must not leave the server up on the other family.

    ``socket.bind`` is read-only, so this holds 127.0.0.1 for real. ``::1``
    can still bind that port. The server must raise and release it.
    """
    import errno

    from compute_service.server import DualStackThreadPoolHTTPServer

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    try:
        with pytest.raises(OSError) as excinfo:
            DualStackThreadPoolHTTPServer(("127.0.0.1", port), _AcceptedConnectionHandler)
        assert excinfo.value.errno == errno.EADDRINUSE
        freed = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            freed.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            freed.bind(("::1", port))
        finally:
            freed.close()
    finally:
        holder.close()


def test_expect_100_continue_before_body() -> None:
    """curl waits about a second unless the server answers 100-continue."""
    from compute_service.server import WSGIDualStackServer

    port = get_free_port()
    seen: dict[str, str] = {}

    def execute_fn(**kwargs):
        seen["code"] = str(kwargs.get("code"))
        return {"status": "ok", "result_json": b'{"status":"ok","result":1,"stdout":""}'}

    app = create_wsgi_app(ComputeSettings(host="127.0.0.1", port=port, workers=1), execute_fn=execute_fn)
    server = WSGIDualStackServer("127.0.0.1", port, max_threads=4)
    server.set_app(app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.15)
    sock: socket.socket | None = None
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        body = b'{"code":"result = 1"}'
        request = (
            "POST /v1/execute HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Expect: 100-continue\r\n"
            "\r\n"
        ).encode()
        sock.sendall(request)
        sock.settimeout(2)
        interim = b""
        while b"\r\n\r\n" not in interim:
            chunk = sock.recv(1024)
            assert chunk
            interim += chunk
        assert interim.startswith(b"HTTP/1.1 100")
        sock.sendall(body)
        rest = b""
        deadline = time.monotonic() + 5
        while b'"status"' not in rest and time.monotonic() < deadline:
            chunk = sock.recv(4096)
            if not chunk:
                break
            rest += chunk
        assert b'"ok"' in rest
        assert seen.get("code") == "result = 1"
    finally:
        if sock is not None:
            sock.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dual_stack_closes_tcpserver_throwaway_socket(monkeypatch) -> None:
    """TCPServer.__init__ opens a socket even when bind_and_activate is False."""
    from compute_service.server import DualStackThreadPoolHTTPServer

    created: list[socket.socket] = []
    real_socket = socket.socket

    def tracking(*args, **kwargs):
        sock = real_socket(*args, **kwargs)
        created.append(sock)
        return sock

    monkeypatch.setattr(socket, "socket", tracking)
    server = DualStackThreadPoolHTTPServer(("127.0.0.1", 0), _AcceptedConnectionHandler, max_threads=1)
    try:
        leaked = [sock for sock in created if sock not in server.sockets and sock.fileno() != -1]
        assert leaked == []
        assert created
        assert any(sock.fileno() == -1 for sock in created)
    finally:
        server.server_close()


def test_run_server_bind_oserror_is_clean(monkeypatch, capsys) -> None:
    """A failed listen prints the address and returns 1, without a traceback."""
    from compute_service.server import main, run_server

    monkeypatch.setattr("compute_service.server.check_dependencies", lambda pool: None)
    monkeypatch.setattr("compute_service.formula_pool.get_formula_pool", lambda settings: MagicMock())
    import plugin.scripting.payload_codec as payload_codec

    monkeypatch.setattr(payload_codec, "load_cython_accelerator", lambda: None)
    monkeypatch.setattr(payload_codec, "get_cython_status_info", lambda: (False, None, "off"))

    def boom(*_args, **_kwargs):
        raise OSError(98, "Address already in use")

    monkeypatch.setattr("compute_service.server.WSGIDualStackServer", boom)
    settings = ComputeSettings(host="127.0.0.1", port=1, workers=1, ocr_workers=0)
    with pytest.raises(OSError):
        run_server(settings)
    err = capsys.readouterr().err
    assert "Failed to bind 127.0.0.1:1" in err
    assert "Address already in use" in err
    assert "Traceback" not in err

    monkeypatch.setattr("compute_service.server.load_settings", lambda **_kwargs: settings)
    assert main([]) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "Failed to bind 127.0.0.1:1" in err


@pytest.mark.skipif(sys.platform == "win32", reason="runs a POSIX shell script")
def test_start_docker_keeps_api_key_as_one_argument(tmp_path) -> None:
    """Spaces and glob characters in the key must stay one docker argument."""
    import subprocess
    from pathlib import Path

    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "args"
    fake = bindir / "docker"
    fake.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" >> \"$DOCKER_ARGS\"\nexit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    script = Path(__file__).resolve().parents[2] / "compute_service" / "start-docker.sh"
    key = "sec ret *"
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "DOCKER_ARGS": str(marker),
        "PYTHON_COMPUTE_API_KEY": key,
        "PYTHON_COMPUTE_IMAGE": "python-compute",
    }
    proc = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    lines = marker.read_text(encoding="utf-8").splitlines()
    assert f"PYTHON_COMPUTE_API_KEY={key}" in lines


@pytest.mark.skipif(sys.platform == "win32", reason="runs a POSIX shell script")
def test_start_docker_mounts_api_key_file_read_only(tmp_path) -> None:
    """The host key file is mounted; the container env points at the mount.

    Forwarding PYTHON_COMPUTE_API_KEY_FILE without -v made the process look
    for the host path inside the image and exit 2. Spaces and glob characters
    in the key and the path must stay one docker argument each.
    """
    import subprocess
    from pathlib import Path

    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "args"
    fake = bindir / "docker"
    fake.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" >> \"$DOCKER_ARGS\"\nexit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    key_file = tmp_path / "sec ret *" / "api key"
    key_file.parent.mkdir()
    key_file.write_text("from-file\n", encoding="utf-8")
    script = Path(__file__).resolve().parents[2] / "compute_service" / "start-docker.sh"
    key = "sec ret *"
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "DOCKER_ARGS": str(marker),
        "PYTHON_COMPUTE_API_KEY": key,
        "PYTHON_COMPUTE_API_KEY_FILE": str(key_file),
        "PYTHON_COMPUTE_IMAGE": "python-compute",
    }
    proc = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    lines = marker.read_text(encoding="utf-8").splitlines()
    mount = f"{key_file}:/run/secrets/python_compute_api_key:ro"
    assert mount in lines
    assert "PYTHON_COMPUTE_API_KEY_FILE=/run/secrets/python_compute_api_key" in lines
    assert f"PYTHON_COMPUTE_API_KEY_FILE={key_file}" not in lines
    assert f"PYTHON_COMPUTE_API_KEY={key}" in lines


def _runner_stage_copies(dockerfile_text: str) -> list[tuple[str, str]]:
    """``COPY src dest`` pairs from the runner stage, skipping ``--from``."""
    stage = ""
    pairs: list[tuple[str, str]] = []
    for raw in dockerfile_text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.upper().startswith("FROM "):
            parts = line.split()
            stage = parts[-1] if len(parts) >= 4 and parts[-2].upper() == "AS" else ""
            continue
        if stage != "runner" or not line.startswith("COPY ") or "--from=" in line:
            continue
        parts = line.split()
        if len(parts) != 3:
            raise AssertionError(f"unexpected COPY form: {line}")
        pairs.append((parts[1], parts[2]))
    return pairs


def test_dockerfile_runner_copy_can_import_worker_base(tmp_path) -> None:
    """The runner COPY set must import worker_base without the rest of plugin/.

    The image copied only framework __init__, constants, and deal_shim.
    worker_base imports worker_pool at load, and that import failed, so the
    container never started. This materializes the Dockerfile COPY lines; it
    does not need a Docker daemon. queue_executor, uno_context, and logging
    are not part of that load-time closure.
    """
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    dockerfile = (repo / "compute_service" / "Dockerfile").read_text(encoding="utf-8")
    pairs = _runner_stage_copies(dockerfile)
    app = tmp_path / "app"
    for src, dest in pairs:
        assert dest.startswith("/app/"), dest
        source = repo / src
        target = app / dest[len("/app/") :]
        assert source.exists(), src
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    framework = app / "plugin" / "framework"
    for name in ("worker_pool.py", "errors.py", "i18n.py", "json_utils.py", "thread_guard.py", "constants.py", "deal_shim.py"):
        assert (framework / name).is_file(), name
    for name in ("queue_executor.py", "uno_context.py", "logging.py"):
        assert not (framework / name).exists(), name

    proc = subprocess.run(
        [sys.executable, "-S", "-P", "-c", "import compute_service.worker_base"],
        cwd=app,
        env={"PYTHONPATH": str(app), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_run_worker_stdio_loop_handles_non_dict_return(monkeypatch) -> None:
    """When a worker handler returns non-dict, run_worker_stdio_loop formats an error dict."""
    from compute_service.worker_base import read_pickle_frame, run_worker_stdio_loop, write_pickle_frame

    stdin_buf = io.BytesIO()
    stdout_buf = io.BytesIO()

    # Write a valid request into stdin_buf
    write_pickle_frame(stdin_buf, {"id": "1", "cmd": "test"})
    stdin_buf.seek(0)

    class MockStdin:
        buffer = stdin_buf

    class MockStdout:
        buffer = stdout_buf

    monkeypatch.setattr("sys.stdin", MockStdin())
    monkeypatch.setattr("sys.stdout", MockStdout())

    def bad_handler(req: dict) -> Any:
        del req
        return "not a dict"

    rc = run_worker_stdio_loop(bad_handler)
    assert rc == 0

    stdout_buf.seek(0)
    ready_frame = read_pickle_frame(stdout_buf)
    assert ready_frame is not None
    assert ready_frame.get("status") == "ready"

    res_frame = read_pickle_frame(stdout_buf)
    assert isinstance(res_frame, dict)
    assert res_frame.get("status") == "error"
    assert res_frame.get("error") == "Handler returned non-dict"


def test_ocr_path_is_allowed_nonexistent_file(tmp_path) -> None:
    """ocr_path_is_allowed checks prefix allowlist even if file does not exist yet."""
    from compute_service.config import ocr_path_is_allowed

    missing = tmp_path / "does_not_exist.png"
    assert ocr_path_is_allowed(str(missing), (str(tmp_path),)) is True
    assert ocr_path_is_allowed(str(missing), ("/other/dir",)) is False


def test_clamp_timeout_sec_infinite_and_nan() -> None:
    """clamp_timeout_sec must handle OverflowError and non-finite floats gracefully."""
    from compute_service.executor import clamp_timeout_sec

    assert clamp_timeout_sec(float("inf"), default_timeout_sec=30) == 30
    assert clamp_timeout_sec(float("-inf"), default_timeout_sec=30) == 30
    assert clamp_timeout_sec(float("nan"), default_timeout_sec=30) == 30
    assert clamp_timeout_sec(1e309, default_timeout_sec=30) == 30


def test_ocr_path_is_allowed_root_directory() -> None:
    """Allowlisted root directory '/' must not fail due to double slash '//'."""
    from compute_service.config import ocr_path_is_allowed

    assert ocr_path_is_allowed("/tmp/image.png", ("/",)) is True
    assert ocr_path_is_allowed("/var/data/doc.pdf", ("/",)) is True


def test_address_string_avoids_reverse_dns() -> None:
    """_DeadlineRequestHandler.address_string must return raw IP without socket.getfqdn()."""
    from compute_service.server import WSGIDualStackServer

    server = WSGIDualStackServer("127.0.0.1", 0, max_threads=1)
    try:
        handler_cls = server.srv.RequestHandlerClass
        with patch("socket.getfqdn") as mock_fqdn:
            handler = handler_cls.__new__(handler_cls)
            handler.client_address = ("127.0.0.1", 54321)
            assert handler.address_string() == "127.0.0.1"
            mock_fqdn.assert_not_called()
    finally:
        server.server_close()


def test_clamp_timeout_sec_boolean() -> None:
    """clamp_timeout_sec must treat booleans as invalid and return default_timeout_sec."""
    from compute_service.executor import clamp_timeout_sec

    assert clamp_timeout_sec(True, default_timeout_sec=30) == 30
    assert clamp_timeout_sec(False, default_timeout_sec=30) == 30


def test_source_text_from_part_unicode_chars() -> None:
    """_validate_source_text must count characters, not bytes, for UTF-8 code parts."""
    from compute_service.server import _validate_source_text
    from compute_service.json_forward import ExecuteRequestError

    # 10 Greek letters (each 2 bytes in UTF-8 = 20 bytes total)
    greek_code = "αβγδεζηθικ".encode("utf-8")
    assert len(greek_code) == 20
    # With limit=10, 10 characters should pass even though byte length is 20
    text = _validate_source_text(greek_code, limit=10, label="code", required=True)
    assert text == "αβγδεζηθικ"

    # With limit=9, 10 characters should be rejected
    with pytest.raises(ExecuteRequestError) as exc_info:
        _validate_source_text(greek_code, limit=9, label="code", required=True)
    assert exc_info.value.code == "CODE_TOO_LARGE"


def test_log_level_cli_arg() -> None:
    """--log-level CLI argument must be parsed and set in ComputeSettings."""
    from compute_service.server import _build_arg_parser, main
    from unittest.mock import patch

    parser = _build_arg_parser()
    args = parser.parse_args(["--log-level", "DEBUG"])
    assert args.log_level == "DEBUG"

    with patch("compute_service.server.run_server") as mock_run:
        assert main(["--log-level", "WARNING"]) == 0
        mock_run.assert_called_once()
        settings = mock_run.call_args[0][0]
        assert settings.log_level == "WARNING"


def test_accept_time_tracked_on_server() -> None:
    """_DeadlineRequestHandler must read accept time from the server's tracking dictionary."""
    from compute_service.server import WSGIDualStackServer

    server = WSGIDualStackServer("127.0.0.1", 0, max_threads=1)
    try:
        handler_cls = server.srv.RequestHandlerClass
        handler = handler_cls.__new__(handler_cls)
        mock_conn = MagicMock()
        handler.connection = mock_conn
        handler.server = server.srv
        handler.server._accept_times[id(mock_conn)] = 12345.678
        handler.client_address = ("127.0.0.1", 54321)
        handler.request_version = "HTTP/1.1"
        handler.command = "GET"
        handler.path = "/health"
        import email.message

        handler.headers = email.message.Message()
        handler.rfile = io.BytesIO(b"")
        with patch.object(handler_cls, "setup"):
            environ = handler.get_environ()
            assert environ.get("compute.accept_time") == 12345.678
    finally:
        server.server_close()
        assert server.srv._accept_times == {}


def test_run_with_logging_deadline_skips_second_status_after_headers() -> None:
    """A raise after start_response must not call start_response again.

    What was wrong: _run_with_logging_and_deadline called _error after action
    had already sent headers. wsgiref raises AssertionError on the second call,
    and _gated never saw the exception.
    """
    from compute_service.server import _run_with_logging_and_deadline

    calls: list[str] = []

    def start_response(status: str, headers: list[tuple[str, str]], exc_info: object = None) -> None:
        del headers, exc_info
        if calls:
            raise AssertionError("Headers already set!")
        calls.append(status)

    def action(start_t: float) -> list[bytes]:
        del start_t
        start_response("200 OK", [("Content-Type", "application/json")])
        raise RuntimeError("late")

    body = _run_with_logging_and_deadline(
        start_response,
        label="/v1/execute",
        req_id="late",
        deadline=time.monotonic() + 5,
        start_msg="exec",
        action=action,
    )
    assert body == []
    assert calls == ["200 OK"]


def test_run_with_logging_deadline_errors_before_headers() -> None:
    from compute_service.server import _run_with_logging_and_deadline

    calls: list[str] = []

    def start_response(status: str, headers: list[tuple[str, str]], exc_info: object = None) -> None:
        del headers, exc_info
        calls.append(status)

    def action(start_t: float) -> list[bytes]:
        del start_t
        raise RuntimeError("early")

    body = _run_with_logging_and_deadline(
        start_response,
        label="/v1/execute",
        req_id="early",
        deadline=time.monotonic() + 5,
        start_msg="exec",
        action=action,
    )
    assert calls == ["500 Internal Server Error"]
    assert body and b"INTERNAL_ERROR" in body[0]


def test_real_socket_queue_timeout() -> None:
    """A blocked worker thread must cause a subsequent request with a short timeout to fail with QUEUE_TIMEOUT."""
    from compute_service.server import WSGIDualStackServer, create_wsgi_app
    from compute_service.config import ComputeSettings
    import threading
    import urllib.request
    import urllib.error
    import json
    import time

    settings = ComputeSettings(
        log_level="DEBUG"
    )

    block_event = threading.Event()

    def fake_execute(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("code") == "block":
            block_event.wait()
            return {"status": "ok"}
        return {"status": "ok"}

    app = create_wsgi_app(settings, execute_fn=fake_execute)
    server = WSGIDualStackServer("127.0.0.1", 0, max_threads=1)

    # Delay processing of the second request to ensure it hits the queue timeout
    original_process_request = server.srv.process_request
    req_count = 0

    def intercept(request: Any, client_address: Any) -> None:
        nonlocal req_count
        req_count += 1
        if req_count > 1:
            time.sleep(1.0)
        original_process_request(request, client_address)

    server.srv.process_request = intercept # type: ignore

    server.set_app(app)

    port = server.srv.server_port
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        req1 = urllib.request.Request(f"http://127.0.0.1:{port}/v1/execute", data=b'{"code": "block", "timeout_ms": 5000}', headers={"Content-Type": "application/json"})
        t1 = threading.Thread(target=lambda: urllib.request.urlopen(req1))
        t1.start()

        # Give the first request time to reach the worker
        time.sleep(0.5)

        req2 = urllib.request.Request(f"http://127.0.0.1:{port}/v1/execute", data=b'{"code": "test", "timeout_ms": 100}', headers={"Content-Type": "application/json"})

        try:
            urllib.request.urlopen(req2)
            assert False, "Expected 503 QUEUE_TIMEOUT, but request succeeded."
        except urllib.error.HTTPError as e:
            assert e.code == 503
            resp = json.loads(e.read().decode("utf-8"))
            assert resp.get("code") == "QUEUE_TIMEOUT"
    finally:
        block_event.set()
        t1.join(timeout=2.0)
        server.shutdown()
        server.server_close()


def test_bearer_scheme_case_insensitive() -> None:
    """RFC 7235: Bearer auth scheme prefix is case-insensitive."""
    settings = ComputeSettings(api_key="my-secret-key")
    app = create_wsgi_app(settings, execute_fn=lambda **kwargs: {"status": "ok", "result": 1})

    for header_val in ("Bearer my-secret-key", "bearer my-secret-key", "BEARER my-secret-key", "BeArEr my-secret-key"):
        status, headers, parsed = _wsgi_post(
            app,
            b'{"code": "result = 1"}',
            path="/v1/execute",
            headers={"Authorization": header_val, "Content-Type": "application/json"},
        )
        assert status == "200 OK", f"Failed for header: {header_val}"
        assert parsed.get("status") == "ok"

    # Malformed prefix fails
    status, headers, parsed = _wsgi_post(
        app,
        b'{"code": "result = 1"}',
        path="/v1/execute",
        headers={"Authorization": "Token my-secret-key", "Content-Type": "application/json"},
    )
    assert status == "401 Unauthorized"


def test_session_reset_passes_bounded_timeout() -> None:
    """The accept-time deadline is the reset lease budget, capped at 5s."""
    seen: list[float] = []

    def fake_reset(sid: str, timeout_sec: float = 5.0) -> dict[str, Any]:
        del sid
        seen.append(timeout_sec)
        return {"status": "ok"}

    app = create_wsgi_app(ComputeSettings(), reset_fn=fake_reset)
    status, _headers, parsed = _wsgi_post(
        app,
        b'{"id": "reset-budget"}',
        path="/v1/session/reset",
        query="session_id=s123",
        headers={"Content-Type": "application/json"},
    )
    assert status == "200 OK"
    assert parsed.get("status") == "ok"
    assert len(seen) == 1
    assert 0 < seen[0] <= 5.0


def test_vision_missing_image_is_400_with_code() -> None:
    """A vision request with no image used to be a generic 400 with no code."""
    app = create_wsgi_app(ComputeSettings())
    payload = json.dumps({"id": "no-image", "helper": "extract_text"}).encode("utf-8")
    status, _headers, body = _wsgi_post(app, payload, path="/v1/vision")
    assert status.startswith("400")
    assert body.get("code") == "MISSING_IMAGE_SOURCE"
    assert body.get("id") == "no-image"


def test_vision_rejects_image_and_file_path_together() -> None:
    app = create_wsgi_app(ComputeSettings())
    payload = json.dumps({"id": "both", "image_b64": "YQ==", "file_path": "/tmp/x.png"}).encode("utf-8")
    status, _headers, body = _wsgi_post(app, payload, path="/v1/vision")
    assert status.startswith("400")
    assert body.get("code") == "INVALID_REQUEST"
    assert body.get("id") == "both"


def test_session_reset_worker_error_returns_500() -> None:
    """Worker-side reset failure is server fault and returns HTTP 500 (not 400)."""
    settings = ComputeSettings()

    def fake_reset(sid: str, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "error", "error": "Worker failed to clear namespace"}

    app = create_wsgi_app(settings, reset_fn=fake_reset)
    status, headers, parsed = _wsgi_post(
        app,
        b'{"id": "test-reset"}',
        path="/v1/session/reset",
        query="session_id=s123",
        headers={"Content-Type": "application/json"},
    )
    assert status == "500 Internal Server Error"
    assert parsed.get("status") == "error"
    assert parsed.get("id") == "test-reset"


def test_session_reset_infra_error_returns_503() -> None:
    """Infrastructure failure during reset returns HTTP 503."""
    settings = ComputeSettings()

    def fake_reset(sid: str, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "error", "code": "WORKER_POOL_BUSY", "error": "Pool busy"}

    app = create_wsgi_app(settings, reset_fn=fake_reset)
    status, headers, parsed = _wsgi_post(
        app,
        b'{"id": "test-reset"}',
        path="/v1/session/reset",
        query="session_id=s123",
        headers={"Content-Type": "application/json"},
    )
    assert status == "503 Service Unavailable"
    assert parsed.get("code") == "WORKER_POOL_BUSY"


def test_mapped_status_json_encode_failure_is_500() -> None:
    """A dumps failure on a mapped code stays inside the response helper.

    The infrastructure status used to be written outside the encode guard,
    so a non-JSON payload escaped and the outer boundary replaced the 503.
    """
    def bad(**_kwargs: Any) -> dict[str, Any]:
        return {"status": "error", "code": "WORKER_POOL_BUSY", "error": object()}

    app = create_wsgi_app(ComputeSettings(), execute_fn=bad)
    status, _headers, body = _wsgi_post(
        app,
        json.dumps({"code": "result = 1"}).encode("utf-8"),
        path="/v1/execute",
    )
    assert status.startswith("500")
    assert "JSON encode failed" in body.get("error", "")


def test_result_too_large_maps_to_413() -> None:
    """Worker returning RESULT_TOO_LARGE maps to HTTP 413 Payload Too Large."""
    settings = ComputeSettings()

    def fake_execute(**kwargs: Any) -> dict[str, Any]:
        return {"id": kwargs.get("req_id"), "status": "error", "code": "RESULT_TOO_LARGE", "error": "IPC frame exceeded 33 MiB"}

    app = create_wsgi_app(settings, execute_fn=fake_execute)
    status, headers, parsed = _wsgi_post(
        app,
        b'{"code": "result = ' + b"x" * 100 + b'"}',
        path="/v1/execute",
        headers={"Content-Type": "application/json"},
    )
    assert status == "413 Payload Too Large"
    assert parsed.get("code") == "RESULT_TOO_LARGE"


def test_session_id_reserved_namespace_rejected() -> None:
    """Session IDs ending in :init or starting with isolated: are rejected with HTTP 400."""
    settings = ComputeSettings()
    app = create_wsgi_app(settings, execute_fn=lambda **kwargs: {"status": "ok"})

    for reserved_sid in ("user-session:init", "isolated:abc123"):
        status, headers, parsed = _wsgi_post(
            app,
            b'{"code": "result = 1", "mode": "shared"}',
            path="/v1/execute",
            query=f"session_id={reserved_sid}",
            headers={"Content-Type": "application/json"},
        )
        assert status == "400 Bad Request"
        assert parsed.get("code") == "INVALID_SESSION_ID"

def test_empty_multi_data_result() -> None:
    payload = {
        "status": "ok",
        "result": {
            "__wa_payload__": "multi_data",
            "items": []
        }
    }
    out = normalize_execute_response(payload)
    assert out["result"] == []


def test_isolated_mode_with_session_id_rejected() -> None:
    """An isolated execute request carrying ?session_id= must return HTTP 400."""
    settings = ComputeSettings()
    app = create_wsgi_app(settings, execute_fn=lambda **kwargs: {"status": "ok"})

    for body in (b'{"code": "result = 1", "mode": "isolated"}', b'{"code": "result = 1"}'):
        status, _headers, parsed = _wsgi_post(
            app,
            body,
            path="/v1/execute",
            query="session_id=valid_sid",
            headers={"Content-Type": "application/json"},
        )
        assert status == "400 Bad Request"
        assert "session_id URL query parameter is only permitted with mode='shared'" in parsed.get("error", "")


def test_header_deadline_fires_inside_handle() -> None:
    """A request line that arrives after the accept deadline must not block for the per-recv timeout."""
    from compute_service.server import DeadlineRequestHandler

    client, server_sock = socket.socketpair()
    try:
        server_sock.settimeout(30)
        rfile = server_sock.makefile("rb")
        wfile = server_sock.makefile("wb", buffering=0)
        handler = DeadlineRequestHandler.__new__(DeadlineRequestHandler)
        handler.connection = server_sock
        handler.rfile = rfile
        handler.wfile = wfile
        handler.client_address = ("127.0.0.1", 0)
        handler.close_connection = True
        handler.server = type("Srv", (), {"_accept_times": {id(server_sock): time.monotonic() - 100.0}})()
        started = time.monotonic()
        with pytest.raises(socket.timeout):
            handler.handle()
        assert time.monotonic() - started < 2.0
    finally:
        client.close()
        server_sock.close()


def test_slow_body_drip_is_408() -> None:
    """One socket timeout per large read used to let a drip outlive the body deadline."""
    from compute_service.server import _REQUEST_READ_TIMEOUT_SEC, _read_request_body

    class _Drip:
        def read1(self, _n: int) -> bytes:
            time.sleep(0.3)
            return b"x"

        def read(self, n: int) -> bytes:
            time.sleep(5)
            return b"x" * n

    statuses: list[str] = []

    def start_response(status: str, _headers: list, _exc_info: Any = None) -> None:
        statuses.append(status)

    environ = {
        "CONTENT_LENGTH": "100000",
        "wsgi.input": _Drip(),
        "compute.accept_time": time.monotonic() - _REQUEST_READ_TIMEOUT_SEC + 0.15,
        "compute.connection": None,
    }
    started = time.monotonic()
    body, err = _read_request_body(environ, ComputeSettings(), start_response)
    assert time.monotonic() - started < 2.0
    assert body is None
    assert err is not None
    assert statuses[0].startswith("408")


def test_drain_restores_socket_timeout() -> None:
    from compute_service.server import _drain_body_before_error

    class _Conn:
        def __init__(self) -> None:
            self.timeout = 30.0

        def gettimeout(self) -> float:
            return self.timeout

        def settimeout(self, value: float) -> None:
            self.timeout = value

    class _Body:
        def read(self, n: int) -> bytes:
            return b"x" * n

    conn = _Conn()
    _drain_body_before_error({"CONTENT_LENGTH": "10", "compute.connection": conn, "wsgi.input": _Body()})
    assert conn.timeout == 30.0


def test_gated_does_not_start_response_twice() -> None:
    """A failure after headers are sent must not call start_response again."""
    calls: list[str] = []

    def start_response(status: str, _headers: list, _exc_info: Any = None) -> None:
        if calls:
            raise AssertionError("start_response called twice")
        calls.append(status)

    def execute_fn(**_kwargs: Any) -> dict[str, Any]:
        return {"status": "ok", "result_json": b'{"status":"ok"}'}

    app = create_wsgi_app(ComputeSettings(), execute_fn=execute_fn)

    def boom(environ: dict, start: Any, settings: ComputeSettings, _run: Any) -> list[bytes]:
        del environ, settings
        start("200 OK", [("Content-Type", "application/json"), ("Content-Length", "2")])
        raise RuntimeError("after headers")

    with patch("compute_service.server._handle_execute", boom):
        out = b"".join(
            app(
                {
                    "PATH_INFO": "/v1/execute",
                    "REQUEST_METHOD": "POST",
                    "QUERY_STRING": "",
                    "CONTENT_LENGTH": "2",
                    "wsgi.input": io.BytesIO(b"{}"),
                },
                start_response,
            )
        )
    assert calls == ["200 OK"]
    assert out == b""


def test_sticky_cap_leaves_isolated_execute_free() -> None:
    """Sticky permits do not consume the isolated-execute gate.

    workers=4 gets one sticky slot per formula worker. Filling those slots
    returns 503 for another sticky call. An isolated call still runs.
    """
    from compute_service.server import sticky_listener_slots

    settings = ComputeSettings(workers=4, ocr_workers=0)
    slots = sticky_listener_slots(settings)
    assert slots == 4
    hold = threading.Event()
    entered = threading.Semaphore(0)

    def execute_fn(**kwargs: Any) -> dict[str, Any]:
        if kwargs.get("session_id"):
            entered.release()
            assert hold.wait(timeout=5)
        return {"status": "ok", "result_json": b'{"status":"ok","result":1}'}

    app = create_wsgi_app(settings, execute_fn=execute_fn)
    body = json.dumps({"code": "result = 1", "mode": "shared"}).encode("utf-8")
    results: list[tuple[str, str | None]] = []
    results_lock = threading.Lock()

    def post_sticky(sid: str) -> None:
        status, _headers, parsed = _wsgi_post(app, body, path="/v1/execute", query=f"session_id={sid}")
        with results_lock:
            results.append((status, parsed.get("code") if isinstance(parsed, dict) else None))

    threads = [threading.Thread(target=post_sticky, args=(f"sticky-{i}",)) for i in range(slots)]
    for thread in threads:
        thread.start()
    try:
        for _unused in range(slots):
            assert entered.acquire(timeout=5)
        status, _headers, parsed = _wsgi_post(app, body, path="/v1/execute", query="session_id=sticky-overflow")
        assert status.startswith("503")
        assert parsed.get("code") == "WORKER_POOL_BUSY"
        isolated, _iheaders, ibody = _wsgi_post(
            app,
            json.dumps({"code": "result = 1"}).encode("utf-8"),
            path="/v1/execute",
        )
        assert isolated.startswith("200")
        assert ibody.get("code") != "WORKER_POOL_BUSY"
    finally:
        hold.set()
        for thread in threads:
            thread.join(timeout=5)
    assert len(results) == slots
    assert all(status.startswith("200") for status, _code in results)


def test_invalid_base64_and_params_are_400() -> None:
    fake_pool = MagicMock()
    fake_pool.execute.return_value = {"id": "b64", "status": "error", "code": "INVALID_BASE64", "error": "bad"}
    app = create_wsgi_app(ComputeSettings(ocr_workers=1))
    payload = json.dumps({"id": "b64", "image_b64": "!!!!"}).encode("utf-8")
    with patch("compute_service.vision_pool.get_vision_pool", return_value=fake_pool):
        status, _headers, body = _wsgi_post(app, payload, path="/v1/vision")
    assert status.startswith("400")
    assert body.get("code") == "INVALID_BASE64"

    params_app = create_wsgi_app(ComputeSettings())
    status, _headers, body = _wsgi_post(
        params_app,
        json.dumps({"image_b64": "YQ==", "params": ["nope"]}).encode("utf-8"),
        path="/v1/vision",
    )
    assert status.startswith("400")
    assert body.get("code") == "INVALID_REQUEST"


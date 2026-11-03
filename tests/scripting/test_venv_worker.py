"""Tests for venv_worker (paths, warm worker, run_code) via PythonWorkerManager + worker_harness."""
from __future__ import annotations
import io
import logging
import os
import pickle
import struct
import subprocess
import sys
import signal
import threading
import time
from typing import Any
from unittest.mock import MagicMock, call, patch
import pytest
from plugin.scripting.config_limits import WARM_WORKER_TIMEOUT_SEC
from plugin.framework.ipc import DEFAULT_MAX_PAYLOAD_BYTES, FRAME_HEARTBEAT, IpcFrameError, IpcPartialFrameTimeout, pack_pickle_frame, read_pickle_frame, unpack_pickle_frame
import plugin.scripting.venv_worker as venv_worker_module
from plugin.framework.subprocess_env import scrub_subprocess_env
from plugin.scripting.session_key import SessionKey
from plugin.scripting.venv_worker import FailedAfterStart, PythonWorkerManager, Terminal, _worker_error, _worker_error_message, _ExecRequest, parse_worker_stdout_frame, reset_python_session, run_code_in_user_venv, shutdown_venv_workers_for_tests, warm_venv_worker
from plugin.framework.process_worker import Deadline
from plugin.framework.warm_child import supervisor
from plugin.scripting.venv.venv_sandbox import run_sandboxed_code, serialize_result

def _mgr_proc(mgr: PythonWorkerManager) -> Any:
    """Test helper: Popen for the live warm child, if any."""
    return mgr._worker_popen()

def _install_mock_proc(mgr: PythonWorkerManager, proc: Any) -> None:
    """Attach a mock Popen to *mgr* for unit tests that stub IPC."""
    warm = MagicMock()
    child = MagicMock()
    child.managed.popen = proc
    warm._child = child
    warm._io_lock = threading.Lock()
    warm.is_alive.return_value = proc.poll() is None if callable(getattr(proc, 'poll', None)) else True
    mgr._warm = warm
    mgr._primed = True
    mgr._primed_generation = 0

def _lease_from_proc(mgr: PythonWorkerManager, proc: Any, *, generation: int=0) -> Any:
    from plugin.framework.warm_child import Lease
    warm = mgr._warm if mgr._warm is not None else MagicMock()
    return Lease(stdin=proc.stdin, stdout=proc.stdout, pid=int(getattr(proc, 'pid', 1) or 1), generation=generation, warm=warm)

def _mgr_stderr_tail(mgr: PythonWorkerManager) -> Any:
    warm = mgr._warm
    if warm is None:
        return None
    child = warm._child
    if child is None:
        return None
    return child.managed.stderr_tail

def _run_ipc_with_mock(mgr: PythonWorkerManager, req: _ExecRequest, proc: Any) -> dict[str, Any]:
    import contextlib
    counter = {'n': 0}

    @contextlib.contextmanager
    def fake_io(*args: object, **kwargs: object):
        del args, kwargs
        counter['n'] += 1
        yield _lease_from_proc(mgr, proc, generation=counter['n'] - 1)

    with patch.object(mgr, '_io_session', fake_io):
        return mgr._run_in_session(lambda leased: mgr._execute_ipc_unlocked(req, leased=leased), timeout_sec=req.timeout_sec or 1)


def _guard_ipc_while_lock_wait(req: _ExecRequest, *, leased: Any) -> dict[str, Any]:
    if req.code == 'result = None' and not req.action:
        return {'status': 'ok', 'result': None}
    raise AssertionError('IPC must not run while waiting on pipe lock')

@pytest.fixture(scope='module', autouse=True)
def _shutdown_workers_after_module():
    """Ensure no leftover worker processes after this file finishes."""
    yield
    shutdown_venv_workers_for_tests()

def _skip_harness_ready_frame(stdout: io.BytesIO) -> None:
    """Consume the warm-child ready frame worker_harness.main emits before the request loop."""
    stdout.seek(0)
    ready = read_pickle_frame(stdout, require_dict=True)
    assert isinstance(ready, dict)
    assert ready.get('status') == 'ready'
    assert 'pid' in ready
_STUB_WORKER_EMIT_READY = '\ndef _emit_ready():\n    import os as _os\n    import pickle as _pickle\n    import struct as _struct\n    _frame = _pickle.dumps({"status": "ready", "pid": _os.getpid()}, protocol=5)\n    sys.stdout.buffer.write(_struct.pack("!I", len(_frame)) + _frame)\n    sys.stdout.buffer.flush()\n\n_emit_ready()\n'

@pytest.fixture(autouse=True)
def _protect_worker_harness_os_exit(monkeypatch: pytest.MonkeyPatch) -> Any:
    import plugin.scripting.venv.worker_harness as harness
    monkeypatch.setattr(harness.os, '_exit', lambda code: pytest.fail(f'Unexpected os._exit({code}) in harness test'))
    try:
        import plugin.contrib.smolagents.local_python_executor as lpe
        lpe.abandoned_timeout = False
    except ImportError:
        pass
    try:
        yield
    finally:
        try:
            import plugin.contrib.smolagents.local_python_executor as lpe
            lpe.abandoned_timeout = False
        except ImportError:
            pass

def test_worker_error_message_strips_command_path():
    long_cmd = ['/very/long/path/to/python', '/very/long/path/to/worker_harness.py']
    exc = subprocess.TimeoutExpired(cmd=long_cmd, timeout=3)
    msg = _worker_error_message(exc)
    assert msg == 'Python worker failed: timed out after 3 seconds'
    assert 'Command' not in msg
    assert '/very/long' not in msg

def test_serialize_numpy_scalar():
    np = pytest.importorskip('numpy')
    assert serialize_result(np.int64(7)) == 7

def test_execute_request_fresh_namespace():
    r1 = run_sandboxed_code('x = 41\nresult = x + 1', None)
    assert r1['status'] == 'ok'
    assert r1['result'] == 42
    r2 = run_sandboxed_code('result = x + 1', None)
    assert r2['status'] == 'error'

def test_execute_request_injects_data():
    r = run_sandboxed_code('result = float(np.sum(data))', [[1, 2, 3, 4]])
    assert r['status'] == 'ok'
    assert r['result'] == 10.0

def test_execute_request_1x1_data_arithmetic_dag():
    r1 = run_sandboxed_code('result = data + 3', [[2]])
    assert r1['status'] == 'ok'
    assert r1['result'] == 5
    r2 = run_sandboxed_code('result = data * 4', [[5.0]])
    assert r2['status'] == 'ok'
    assert r2['result'] == 20.0

def test_execute_request_1x1_data_serialize_unwraps_to_scalar():
    r = run_sandboxed_code('result = data', [[2]])
    assert r['status'] == 'ok'
    assert r['result'] == 2
    assert not isinstance(r['result'], list)

def test_execute_request_fan_out_dag_returns_scalars():
    r1 = run_sandboxed_code('result = data', [[2]])
    r2 = run_sandboxed_code('result = data', [[2]])
    assert r1['status'] == 'ok' and r1['result'] == 2
    assert r2['status'] == 'ok' and r2['result'] == 2

def test_execute_request_injects_ranges_single_range():
    r = run_sandboxed_code("result = (len(ranges), data is ranges[0], hasattr(data, 'to_pandas'))", [[1, 2, 3]])
    assert r['status'] == 'ok'
    assert r['result'] == [1, True, True]

def test_execute_request_injects_ranges_multi_polymorphic_data():
    from plugin.calc.calc_addin_data import pack_calc_multi_data_for_wire
    wire = pack_calc_multi_data_for_wire([[[1.0, 2.0, 3.0]], [[4.0, 5.0]]], force='never')
    r = run_sandboxed_code('result = (len(ranges), data is ranges, data[1].values[0][0])', wire)
    assert r['status'] == 'ok'
    assert r['result'] == [2, True, 4.0]

def test_execute_request_does_not_inject_inputs():
    r = run_sandboxed_code('result = inputs', [[1]])
    assert r['status'] == 'error'
    assert 'inputs' in r.get('message', '').lower() and 'not defined' in r.get('message', '').lower()

def test_run_code_in_user_venv_passes_stop_checker():
    """run_code_in_user_venv must pass stop_checker to the manager's execute method."""
    with patch('plugin.scripting.venv_worker._get_worker_manager') as mock_mgr_ctx:
        mock_mgr = MagicMock()
        mock_mgr.execute.return_value = {'status': 'ok'}
        mock_mgr_ctx.return_value = (mock_mgr, None)

        def stop_fn() -> bool:
            return True
        run_code_in_user_venv(code='result = 1', stop_checker=stop_fn)
        mock_mgr.execute.assert_called_once()
        kwargs = mock_mgr.execute.call_args.kwargs
        assert kwargs.get('stop_checker') is stop_fn

def test_blocked_import_os():
    r = run_sandboxed_code('import subprocess\nresult = 1', None)
    assert r['status'] == 'error'
    assert 'not allowed' in r.get('message', '').lower() or 'Import' in r.get('message', '')

def test_blocked_import_not_on_allowlist():
    pytest.importorskip('requests')
    r = run_sandboxed_code('import requests\nresult = 1', None)
    assert r['status'] == 'error'
    assert 'not allowed' in r.get('message', '').lower() or 'Import' in r.get('message', '')

def test_sentence_transformers_import_not_deep_wrapped():
    """Heavy embedder packages must bypass get_safe_module scanning (hangs on dir()/getattr)."""
    st = pytest.importorskip('sentence_transformers')
    from plugin.contrib.smolagents.local_python_executor import get_safe_module
    assert get_safe_module(st, []) is st
    r = run_sandboxed_code('from sentence_transformers import SentenceTransformer\nresult = str(SentenceTransformer)', None)
    assert r['status'] == 'ok'
    assert 'SentenceTransformer' in r['result']

def test_duckdb_import_not_deep_wrapped():
    """DuckDB (C-backed analytics lib) must bypass get_safe_module like other heavy packages."""
    duck = pytest.importorskip('duckdb')
    from plugin.contrib.smolagents.local_python_executor import get_safe_module
    assert get_safe_module(duck, []) is duck
    r = run_sandboxed_code("import duckdb\ncon = duckdb.connect()\nresult = con.execute('SELECT 42 AS x').df().to_dict()", None)
    assert r['status'] == 'ok'
    assert 'x' in str(r.get('result', '')) or 42 in str(r.get('result', ''))

def test_harness_main_loop_integration():
    """Harness reads and writes Pickle (subprocess smoke)."""
    harness = __import__('plugin.scripting.venv.worker_harness', fromlist=['main'])
    proc_pickle = subprocess.Popen([sys.executable, harness.__file__], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False, bufsize=0)
    req_dict = {'id': 't2', 'code': 'result = 2 ** 10'}
    proc_pickle.stdin.write(pack_pickle_frame(req_dict))
    proc_pickle.stdin.flush()
    ready = read_pickle_frame(proc_pickle.stdout, require_dict=True)
    assert ready is not None and ready.get('status') == 'ready'
    started = read_pickle_frame(proc_pickle.stdout, require_dict=True)
    assert started is not None
    assert started['type'] == 'exec_started'
    assert started['id'] == 't2'
    resp_dict = read_pickle_frame(proc_pickle.stdout, require_dict=True)
    assert resp_dict is not None
    assert resp_dict['id'] == 't2'
    assert resp_dict['status'] == 'ok'
    assert resp_dict['result'] == 1024
    proc_pickle.stdin.close()
    proc_pickle.wait(timeout=5)

def test_manager_real_spawn_drains_stderr_flood(tmp_path, monkeypatch):
    """The manager's actual Popen path must drain stderr before waiting for a response."""
    import plugin.scripting.venv_worker as venv_worker_module
    child = tmp_path / 'stderr_flood_worker.py'
    child.write_text(('\nimport pickle\nimport struct\nimport sys\n\nsys.stderr.buffer.write(b"x" * (128 * 1024))\nsys.stderr.buffer.flush()\n\n' + _STUB_WORKER_EMIT_READY + '\nwhile True:\n    header = sys.stdin.buffer.read(4)\n    if len(header) < 4:\n        break\n    size = struct.unpack("!I", header)[0]\n    payload = sys.stdin.buffer.read(size)\n    if len(payload) < size:\n        break\n    request = pickle.loads(payload)\n    response = {"id": request.get("id"), "status": "ok", "result": 42, "stdout": ""}\n    encoded = pickle.dumps(response, protocol=5)\n    sys.stdout.buffer.write(struct.pack("!I", len(encoded)) + encoded)\n    sys.stdout.buffer.flush()\n').lstrip(), encoding='utf-8')
    monkeypatch.setattr(venv_worker_module, '_HARNESS_PATH', str(child))
    monkeypatch.setattr(venv_worker_module, 'wrap_command_for_sandbox', lambda cmd, **_kwargs: cmd)
    mgr = PythonWorkerManager(sys.executable, {'PATH': os.environ.get('PATH', '')})
    drain = None
    try:
        result = mgr.execute('result = 42', timeout_sec=5)
        drain = _mgr_stderr_tail(mgr)
        assert result['status'] == 'ok'
        assert result['result'] == 42
        assert drain is not None
        deadline = time.monotonic() + 2.0
        captured = ''
        while time.monotonic() < deadline:
            captured = drain.text()
            if 'x' * 1024 in captured:
                break
            time.sleep(0.01)
        assert 'x' * 1024 in captured
    finally:
        mgr._terminate_worker()
    assert drain is not None
    assert not drain.is_alive

def test_worker_crash_mid_request_retries_and_recovers(tmp_path, monkeypatch):
    """A child that dies once mid-IPC must be recycled; the retried turn should succeed."""
    import plugin.scripting.venv_worker as venv_worker_module
    crash_once = tmp_path / 'crash_once'
    crash_once.write_text('1', encoding='utf-8')
    child = tmp_path / 'crash_once_worker.py'
    child.write_text((f'\nimport os\nimport pickle\nimport struct\nimport sys\n\nflag = {str(crash_once)!r}\n\n' + _STUB_WORKER_EMIT_READY + '\nwhile True:\n    header = sys.stdin.buffer.read(4)\n    if len(header) < 4:\n        break\n    size = struct.unpack("!I", header)[0]\n    payload = sys.stdin.buffer.read(size)\n    if len(payload) < size:\n        break\n    if os.path.exists(flag):\n        os.remove(flag)\n        os._exit(1)\n    request = pickle.loads(payload)\n    response = {"id": request.get("id"), "status": "ok", "result": 42, "stdout": ""}\n    encoded = pickle.dumps(response, protocol=5)\n    sys.stdout.buffer.write(struct.pack("!I", len(encoded)) + encoded)\n    sys.stdout.buffer.flush()\n').lstrip(), encoding='utf-8')
    monkeypatch.setattr(venv_worker_module, '_HARNESS_PATH', str(child))
    monkeypatch.setattr(venv_worker_module, 'wrap_command_for_sandbox', lambda cmd, **_kwargs: cmd)
    mgr = PythonWorkerManager(sys.executable, {'PATH': os.environ.get('PATH', '')})
    try:
        result = mgr.execute('result = 42', timeout_sec=5)
        assert result['status'] == 'ok'
        assert result['result'] == 42
        assert _mgr_proc(mgr) is not None and _mgr_proc(mgr).poll() is None
    finally:
        mgr._terminate_worker()

def test_worker_child_dies_without_response_fails_promptly(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A child that dies without writing a response returns promptly with an error and retries once."""
    shutdown_venv_workers_for_tests()
    counter_file = tmp_path / 'counter.txt'
    child = tmp_path / 'die_worker.py'
    child.write_text(('\nimport os\nimport struct\nimport sys\n\n' + _STUB_WORKER_EMIT_READY + f'\nwith open({str(counter_file)!r}, "a") as f:\n    f.write("start\\n")\n    f.flush()\n\nheader = sys.stdin.buffer.read(4)\nif len(header) == 4:\n    size = struct.unpack("!I", header)[0]\n    sys.stdin.buffer.read(size)\nos._exit(0)\n').lstrip(), encoding='utf-8')
    monkeypatch.setattr(venv_worker_module, '_HARNESS_PATH', str(child))
    monkeypatch.setattr(venv_worker_module, 'wrap_command_for_sandbox', lambda cmd, **_kwargs: cmd)
    monkeypatch.setattr(venv_worker_module, 'WARM_WORKER_TIMEOUT_SEC', 8.0)
    monkeypatch.setattr(venv_worker_module, 'HOST_IPC_READ_GRACE_SEC', 8.0)
    mgr = PythonWorkerManager(sys.executable, {'PATH': os.environ.get('PATH', '')})
    mgr._ensure_warmed_unlocked = lambda *args, **kwargs: None
    t0 = time.monotonic()
    try:
        res = mgr.execute('result = 1', timeout_sec=8)
        elapsed = time.monotonic() - t0
        assert elapsed < 4.0
        assert res.get('status') == 'error'
        assert res.get('code') == 'WORKER_IPC_ERROR'
        assert 'without a response' in res.get('message', '')
        assert counter_file.read_text(encoding='utf-8').splitlines() == ['start', 'start']
        assert _mgr_proc(mgr) is None or _mgr_proc(mgr).poll() is not None
    finally:
        mgr._terminate_worker()

def test_worker_child_hung_zero_bytes_times_out_without_replay(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A live child that hangs without writing times out within window and does not replay."""
    counter_file = tmp_path / 'counter.txt'
    child = tmp_path / 'hung_zero_worker.py'
    child.write_text(('\nimport os\nimport struct\nimport sys\nimport time\n\n' + _STUB_WORKER_EMIT_READY + f'\nwith open({str(counter_file)!r}, "a") as f:\n    f.write("start\\n")\n    f.flush()\n\nheader = sys.stdin.buffer.read(4)\nif len(header) == 4:\n    size = struct.unpack("!I", header)[0]\n    sys.stdin.buffer.read(size)\nheader = sys.stdin.buffer.read(4)\nif len(header) == 4:\n    size = struct.unpack("!I", header)[0]\n    sys.stdin.buffer.read(size)\ntime.sleep(120)\n').lstrip(), encoding='utf-8')
    monkeypatch.setattr(venv_worker_module, '_HARNESS_PATH', str(child))
    monkeypatch.setattr(venv_worker_module, 'wrap_command_for_sandbox', lambda cmd, **_kwargs: cmd)
    monkeypatch.setattr(venv_worker_module, 'WARM_WORKER_TIMEOUT_SEC', 1.0)
    monkeypatch.setattr(venv_worker_module, 'HOST_IPC_READ_GRACE_SEC', 0.5)
    mgr = PythonWorkerManager(sys.executable, {'PATH': os.environ.get('PATH', '')})
    t0 = time.monotonic()
    try:
        res = mgr.execute('result = 1', timeout_sec=1)
        elapsed = time.monotonic() - t0
        assert 1.0 <= elapsed < 4.0
        assert res.get('status') == 'error'
        assert res.get('code') == 'VENV_TIMEOUT'
        assert 'timed out after 1.5 seconds' in res.get('message', '')
        assert counter_file.read_text(encoding='utf-8').splitlines() == ['start']
    finally:
        mgr._terminate_worker()

def test_worker_child_hung_partial_frame_times_out_without_replay(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A live child that writes a partial frame and hangs times out without replay."""
    counter_file = tmp_path / 'counter.txt'
    child = tmp_path / 'hung_partial_worker.py'
    child.write_text(('\nimport os\nimport struct\nimport sys\nimport time\n\n' + _STUB_WORKER_EMIT_READY + f'\nwith open({str(counter_file)!r}, "a") as f:\n    f.write("start\\n")\n    f.flush()\n\nheader = sys.stdin.buffer.read(4)\nif len(header) == 4:\n    size = struct.unpack("!I", header)[0]\n    sys.stdin.buffer.read(size)\nheader = sys.stdin.buffer.read(4)\nif len(header) == 4:\n    size = struct.unpack("!I", header)[0]\n    sys.stdin.buffer.read(size)\nheader = sys.stdin.buffer.read(4)\nif len(header) == 4:\n    size = struct.unpack("!I", header)[0]\n    sys.stdin.buffer.read(size)\nsys.stdout.buffer.write(b"\\x00\\x00")\nsys.stdout.buffer.flush()\ntime.sleep(120)\n').lstrip(), encoding='utf-8')
    monkeypatch.setattr(venv_worker_module, '_HARNESS_PATH', str(child))
    monkeypatch.setattr(venv_worker_module, 'wrap_command_for_sandbox', lambda cmd, **_kwargs: cmd)
    monkeypatch.setattr(venv_worker_module, 'WARM_WORKER_TIMEOUT_SEC', 1.0)
    monkeypatch.setattr(venv_worker_module, 'HOST_IPC_READ_GRACE_SEC', 0.5)
    mgr = PythonWorkerManager(sys.executable, {'PATH': os.environ.get('PATH', '')})
    t0 = time.monotonic()
    try:
        res = mgr.execute('result = 1', timeout_sec=1)
        elapsed = time.monotonic() - t0
        assert 1.0 <= elapsed < 4.0
        assert res.get('status') == 'error'
        assert res.get('code') == 'VENV_TIMEOUT'
        assert 'mid-frame' in res.get('message', '') or 'timed out' in res.get('message', '')
        assert counter_file.read_text(encoding='utf-8').splitlines() == ['start']
    finally:
        mgr._terminate_worker()

def pid_is_alive(pid: int) -> bool:
    """True if *pid* still names a live process.

    ``os.kill(pid, 0)`` is POSIX. On Windows signal 0 is WinError 87, so a
    naive helper treated every live grandchild as dead (CI 33453184665).
    Production kill code does not call this; the grandchild tests do.
    """
    if pid <= 0:
        return False
    if sys.platform == 'win32':
        return _pid_is_alive_win32(int(pid))
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True

def _pid_is_alive_win32(pid: int) -> bool:
    if sys.platform != 'win32':
        return False
    import ctypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    handle = kernel32.OpenProcess(4096, False, pid)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return ctypes.get_last_error() == 5

def test_pid_is_alive_current_process():
    assert pid_is_alive(os.getpid())
    assert not pid_is_alive(0)
    assert not pid_is_alive(-1)

def test_terminate_worker_kills_grandchild(tmp_path, monkeypatch):
    """Timeout/crash cleanup must kill descendants (joblib/loky, DataLoader), not only the worker."""
    import plugin.scripting.venv_worker as venv_worker_module
    child = tmp_path / 'tree_worker.py'
    child.write_text(('\nimport os\nimport subprocess\nimport sys\nimport time\n\n' + _STUB_WORKER_EMIT_READY + '\ngrandchild = subprocess.Popen(\n    [sys.executable, "-c", "import time; time.sleep(120)"],\n    stdout=subprocess.DEVNULL,\n    stderr=subprocess.DEVNULL,\n)\nsys.stderr.write(f"GRANDCHILD {grandchild.pid}\\n")\nsys.stderr.flush()\ntime.sleep(120)\n').lstrip(), encoding='utf-8')
    monkeypatch.setattr(venv_worker_module, '_HARNESS_PATH', str(child))
    monkeypatch.setattr(venv_worker_module, 'wrap_command_for_sandbox', lambda cmd, **_kwargs: cmd)
    mgr = PythonWorkerManager(sys.executable, {'PATH': os.environ.get('PATH', '')})
    monkeypatch.setattr(mgr, '_ensure_warmed_unlocked', lambda leased: None)
    gpid = None
    try:
        mgr._ensure_running()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            drain = _mgr_stderr_tail(mgr)
            text = drain.text() if drain is not None else ''
            for line in text.splitlines():
                if line.startswith('GRANDCHILD '):
                    gpid = int(line.split()[1])
                    break
            if gpid is not None:
                break
            time.sleep(0.05)
        assert gpid is not None, 'worker did not report grandchild pid'
        assert pid_is_alive(gpid)
        mgr._terminate_worker()
        dead_deadline = time.monotonic() + 5
        while time.monotonic() < dead_deadline and pid_is_alive(gpid):
            time.sleep(0.05)
        assert not pid_is_alive(gpid), f'grandchild pid {gpid} survived worker termination'
    finally:
        mgr._terminate_worker()
        if gpid is not None and pid_is_alive(gpid):
            try:
                os.kill(gpid, 9)
            except OSError:
                pass

def test_blocked_stdin_write_times_out_and_releases_lock(tmp_path, monkeypatch):
    """A child that never reads stdin must not hold the warm-child pipe lock forever."""
    from plugin.framework.process_worker import Deadline
    from plugin.framework.warm_child import ChildSpec, WarmChild
    import plugin.scripting.venv_worker as venv_worker_module
    child = tmp_path / 'blocked_stdin_worker.py'
    child.write_text(('import sys\n' + _STUB_WORKER_EMIT_READY + 'import time\ntime.sleep(60)\n').lstrip(), encoding='utf-8')
    monkeypatch.setattr(venv_worker_module, 'wrap_command_for_sandbox', lambda cmd, **_kwargs: cmd)
    env = {k: v for k, v in os.environ.items() if isinstance(v, str)}
    env['WRITERAGENT_IS_WORKER'] = '1'
    spec = ChildSpec(
        name='blocked-stdin-test',
        argv=[sys.executable, str(child)],
        env=env,
        command_wrapper=venv_worker_module.wrap_command_for_sandbox,
    )
    warm = WarmChild(spec)
    mgr = PythonWorkerManager.__new__(PythonWorkerManager)
    mgr.exe = sys.executable
    mgr._warm = warm
    try:
        with warm.lease(Deadline(30)) as leased:
            assert not isinstance(leased, dict)
            started = time.monotonic()
            with pytest.raises(subprocess.TimeoutExpired):
                mgr._write_packed_frame_with_timeout(leased.stdin, b'x' * (8 * 1024 * 1024), timeout_sec=0.2, label='test request')
            assert time.monotonic() - started < 5
        for t in threading.enumerate():
            if t.name == 'ipc-stdin-write':
                t.join(timeout=5)
                assert not t.is_alive()
        assert warm._io_lock.acquire(timeout=1)
        warm._io_lock.release()
    finally:
        warm.retire()

def test_execute_refuses_reentry_while_this_thread_owns_the_pipe():
    mgr = PythonWorkerManager(sys.executable, {})
    _install_mock_proc(mgr, MagicMock(poll=MagicMock(return_value=None), stdin=io.BytesIO(), stdout=io.BytesIO()))
    mgr._io_owner = threading.get_ident()
    result = mgr.execute('result = 1', timeout_sec=1)
    assert result['status'] == 'error'
    assert result['code'] == 'WORKER_REENTRY'
    assert 'called back' in result['message']
    assert not mgr._io_lock.locked()

def test_large_stdin_write_completes_intact():
    mgr = PythonWorkerManager(sys.executable, {})
    stream = io.BytesIO()
    payload = b'large-payload-' * (256 * 1024)
    mgr._write_packed_frame_with_timeout(stream, payload, timeout_sec=2, label='test request')
    assert stream.getvalue() == payload

def test_initial_write_timeout_retries_once():
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    _install_mock_proc(mgr, proc)
    mgr._ensure_running = MagicMock()
    mgr._write_frame_with_timeout = MagicMock(side_effect=subprocess.TimeoutExpired(cmd=sys.executable, timeout=1))
    mgr._terminate_worker = MagicMock()
    result = _run_ipc_with_mock(mgr, _ExecRequest('result = 1', timeout_sec=1), proc)
    assert result['status'] == 'error'
    assert 'timed out after 1 seconds' in result['message']
    assert mgr._write_frame_with_timeout.call_count == 2
    assert mgr._terminate_worker.call_count == 2
    first = mgr._write_frame_with_timeout.call_args_list[0].args[1]
    second = mgr._write_frame_with_timeout.call_args_list[1].args[1]
    assert first['id'] == second['id']

def test_ppt_master_write_timeout_does_not_replay(monkeypatch):
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    _install_mock_proc(mgr, proc)
    mgr._ensure_running = MagicMock()
    mgr._write_frame_with_timeout = MagicMock()
    mgr._write_packed_frame_with_timeout = MagicMock(side_effect=subprocess.TimeoutExpired(cmd=sys.executable, timeout=1))
    response_payload = pickle.dumps({'status': 'host_request'}, protocol=5)
    mgr._read_response_bytes = MagicMock(return_value=response_payload)
    mgr._terminate_worker = MagicMock()
    dispatch_calls = []

    def dispatch(response, *, stdin_write, on_worker_event=None, stop_checker=None, cancellation_scope=None):
        del response, on_worker_event, stop_checker, cancellation_scope
        dispatch_calls.append(True)
        stdin_write(b'host response')
        return True
    monkeypatch.setattr('plugin.ppt_master.venv.host_rpc.dispatch_worker_response', dispatch)
    result = _run_ipc_with_mock(mgr, _ExecRequest('result = 1', timeout_sec=1, caller='ppt_master_venv'), proc)
    assert result['status'] == 'error'
    assert 'host RPC response timed out' in result['message']
    assert len(dispatch_calls) == 1
    assert mgr._write_frame_with_timeout.call_count == 1
    assert mgr._read_response_bytes.call_count == 1
    assert mgr._terminate_worker.call_count == 1

def test_tool_call_then_broken_stdout_does_not_replay(monkeypatch):
    """A tool_call that already ran must not be followed by a second copy of the script."""
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    _install_mock_proc(mgr, proc)
    mgr._ensure_running = MagicMock()
    mgr._write_frame_with_timeout = MagicMock()
    response_payload = pickle.dumps({'status': 'host_request'}, protocol=5)
    mgr._read_response_bytes = MagicMock(side_effect=[response_payload, RuntimeError('Worker closed stdout')])
    mgr._terminate_worker = MagicMock()

    def dispatch(response, *, stdin_write, on_worker_event=None, stop_checker=None, cancellation_scope=None):
        del response, stdin_write, on_worker_event, stop_checker, cancellation_scope
        return True
    monkeypatch.setattr('plugin.ppt_master.venv.host_rpc.dispatch_worker_response', dispatch)
    result = _run_ipc_with_mock(mgr, _ExecRequest('result = 1', timeout_sec=1, caller='ppt_master_venv'), proc)
    assert result['status'] == 'error'
    assert mgr._write_frame_with_timeout.call_count == 1
    assert mgr._terminate_worker.call_count == 1

@pytest.mark.parametrize('frame_type', ['tool_call', 'llm_request', 'worker_event'])
def test_tool_call_raise_without_exec_started_does_not_replay(monkeypatch, frame_type):
    """An intermediate frame that raises mid-dispatch must not be sent again.

    The flag used to be set only after dispatch returned. A RuntimeError
    from the handler, with no exec_started frame, left may_have_run false
    and the attempt loop wrote the same request to a new child.
    """
    import plugin.scripting.venv_worker as venv_worker_module
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    _install_mock_proc(mgr, proc)
    mgr._ensure_running = MagicMock()
    mgr._write_frame_with_timeout = MagicMock()
    mgr._read_response_bytes = MagicMock(return_value=pickle.dumps({'type': frame_type, 'id': 't1', 'tool': 'apply_document_content', 'args': {}}, protocol=5))
    mgr._terminate_worker = MagicMock()

    def dispatch(*args, **kwargs):
        del args, kwargs
        raise RuntimeError('tool mutated then failed')
    monkeypatch.setattr(venv_worker_module, '_maybe_dispatch_intermediate_response', dispatch)
    result = _run_ipc_with_mock(mgr, _ExecRequest('result = 1', timeout_sec=1), proc)
    assert result['status'] == 'error'
    assert result['code'] == 'WORKER_IPC_ERROR'
    assert 'tool mutated then failed' in result['message']
    assert mgr._write_frame_with_timeout.call_count == 1
    assert mgr._terminate_worker.call_count == 1

def test_stdout_close_before_start_retries_once():
    """A death before exec_started has not run the request; recycle the child."""
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    _install_mock_proc(mgr, proc)
    mgr._ensure_running = MagicMock()
    mgr._write_frame_with_timeout = MagicMock()
    mgr._read_response_bytes = MagicMock(return_value=b'')
    mgr._drain_stderr = MagicMock(return_value='')
    mgr._terminate_worker = MagicMock()
    result = _run_ipc_with_mock(mgr, _ExecRequest('result = 1', timeout_sec=1), proc)
    assert result['status'] == 'error'
    assert 'without a response' in result['message']
    assert mgr._write_frame_with_timeout.call_count == 2
    assert mgr._terminate_worker.call_count == 2
    assert mgr._terminate_worker.call_args_list == [call(), call()]

def test_truncated_header_before_exec_started_fails_without_replay():
    """A truncated frame is stream desynchronization; do not replay."""
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO(b'\x00\x00')
    _install_mock_proc(mgr, proc)
    mgr._ensure_running = MagicMock()
    mgr._write_frame_with_timeout = MagicMock()
    mgr._drain_stderr = MagicMock(return_value='')
    mgr._terminate_worker = MagicMock()
    result = _run_ipc_with_mock(mgr, _ExecRequest('result = 1', timeout_sec=1), proc)
    assert result['status'] == 'error'
    assert result['code'] in ('WORKER_IPC_ERROR', 'VENV_TIMEOUT')
    assert mgr._write_frame_with_timeout.call_count == 1
    assert mgr._terminate_worker.call_count == 1
    assert mgr._terminate_worker.call_args_list == [call()]

def test_runtime_error_before_exec_started_retries_once():
    """A RuntimeError before the child has started the script is still a failed start."""
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    _install_mock_proc(mgr, proc)
    mgr._ensure_running = MagicMock()
    mgr._write_frame_with_timeout = MagicMock()
    mgr._read_response_bytes = MagicMock(side_effect=RuntimeError('pipe died'))
    mgr._terminate_worker = MagicMock()
    result = _run_ipc_with_mock(mgr, _ExecRequest('result = 1', timeout_sec=1), proc)
    assert result['status'] == 'error'
    assert 'pipe died' in result['message']
    assert mgr._write_frame_with_timeout.call_count == 2
    assert mgr._terminate_worker.call_count == 2
    assert mgr._terminate_worker.call_args_list == [call(), call()]

def test_runtime_error_after_exec_started_does_not_replay():
    """A RuntimeError after exec_started must not resend the script on a new child."""
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    _install_mock_proc(mgr, proc)
    mgr._ensure_running = MagicMock()
    captured: dict[str, dict] = {}

    def _write_frame(stdin, request, **kwargs):
        captured['request'] = request
    mgr._write_frame_with_timeout = _write_frame
    reads = {'n': 0}

    def _read(stdout, timeout_sec, stop_checker=None):
        reads['n'] += 1
        request = captured['request']
        if reads['n'] == 1:
            return pickle.dumps({'type': 'exec_started', 'id': request['id']}, protocol=5)
        raise RuntimeError('worker frame was not a dict')
    mgr._read_response_bytes = _read
    mgr._terminate_worker = MagicMock()
    result = _run_ipc_with_mock(mgr, _ExecRequest('result = 1', timeout_sec=1), proc)
    assert result['status'] == 'error'
    assert result['code'] == 'WORKER_IPC_ERROR'
    assert 'not a dict' in result['message']
    assert reads['n'] == 2
    assert mgr._terminate_worker.call_count == 1

def test_stdout_close_after_exec_started_does_not_replay():
    """In-process side effects after exec_started must not run under the same id again."""
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    _install_mock_proc(mgr, proc)
    mgr._ensure_running = MagicMock()
    captured: dict[str, dict] = {}

    def _write_frame(stdin, request, **kwargs):
        captured['request'] = request
    mgr._write_frame_with_timeout = _write_frame
    reads = {'n': 0}

    def _read(stdout, timeout_sec, stop_checker=None):
        reads['n'] += 1
        request = captured['request']
        if reads['n'] == 1:
            return pickle.dumps({'type': 'exec_started', 'id': request['id']}, protocol=5)
        return b''
    mgr._read_response_bytes = _read
    mgr._drain_stderr = MagicMock(return_value='')
    mgr._terminate_worker = MagicMock()
    result = _run_ipc_with_mock(mgr, _ExecRequest('result = 1', timeout_sec=1), proc)
    assert result['status'] == 'error'
    assert result['code'] == 'WORKER_IPC_ERROR'
    assert 'without a response' in result['message']
    assert 'request' in captured
    assert reads['n'] == 2
    assert mgr._terminate_worker.call_count == 1

def test_exec_started_wrong_id_is_refused_without_replay():
    """An exec_started for another request is an id mismatch: kill, no resend."""
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    _install_mock_proc(mgr, proc)
    mgr._ensure_running = MagicMock()
    mgr._write_frame_with_timeout = MagicMock()
    mgr._read_response_bytes = MagicMock(return_value=pickle.dumps({'type': 'exec_started', 'id': 'someone-else'}, protocol=5))
    mgr._terminate_worker = MagicMock()
    result = _run_ipc_with_mock(mgr, _ExecRequest('result = 1', timeout_sec=1), proc)
    assert result['code'] == 'WORKER_IPC_ERROR'
    assert 'id mismatch' in result['message']
    assert mgr._write_frame_with_timeout.call_count == 1
    assert mgr._terminate_worker.call_count == 1

def test_host_read_timeout_does_not_retry():
    """Hung user code must not be replayed; that would double the configured timeout."""
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    _install_mock_proc(mgr, proc)
    mgr._ensure_running = MagicMock()
    mgr._write_frame_with_timeout = MagicMock()
    mgr._read_response_bytes = MagicMock(side_effect=subprocess.TimeoutExpired(cmd=sys.executable, timeout=1))
    mgr._terminate_worker = MagicMock()
    result = _run_ipc_with_mock(mgr, _ExecRequest('result = 1', timeout_sec=1), proc)
    assert result['status'] == 'error'
    assert 'timed out after 1 seconds' in result['message']
    assert mgr._write_frame_with_timeout.call_count == 1
    assert mgr._read_response_bytes.call_count == 1
    assert mgr._terminate_worker.call_count == 1

def test_partial_frame_timeout_does_not_replay():
    """Windows mid-frame stall must not resend the script.

    IpcPartialFrameTimeout is an OSError. Before it was mapped with the
    read timeout, a partial exec_started left may_have_run false and the
    outer handler ran the request again.
    """
    from plugin.framework.ipc import IpcPartialFrameTimeout
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    _install_mock_proc(mgr, proc)
    mgr._ensure_running = MagicMock()
    mgr._write_frame_with_timeout = MagicMock()
    mgr._read_response_bytes = MagicMock(side_effect=IpcPartialFrameTimeout('python stream desynchronized: timeout mid-frame'))
    mgr._terminate_worker = MagicMock()
    result = _run_ipc_with_mock(mgr, _ExecRequest('result = 1', timeout_sec=1), proc)
    assert result['status'] == 'error'
    assert result['code'] == 'VENV_TIMEOUT'
    assert 'mid-frame' in result['message']
    assert mgr._write_frame_with_timeout.call_count == 1
    assert mgr._read_response_bytes.call_count == 1
    mgr._terminate_worker.assert_called_once()

def test_win32_partial_frame_timeout_with_stop_is_cancelled(monkeypatch: pytest.MonkeyPatch):
    """Stop during a Windows mid-frame stall is CANCELLED, not a replayed timeout."""
    monkeypatch.setattr(sys, 'platform', 'win32')
    mgr = PythonWorkerManager.__new__(PythonWorkerManager)
    mgr.exe = 'python'
    _install_mock_proc(mgr, MagicMock(poll=MagicMock(return_value=None)))

    class _Stdout(io.BytesIO):

        def fileno(self) -> int:
            return 3

    def _boom(*args: object, **kwargs: object) -> bytes:
        del args, kwargs
        raise IpcPartialFrameTimeout('timeout mid-frame')
    from plugin.scripting import venv_worker as venv_worker_mod
    ipc_module = sys.modules[venv_worker_mod.read_frame_payload.__module__]
    monkeypatch.setattr(ipc_module, '_poll_pipe', _boom)
    res = mgr._read_frame_bytes(_Stdout(), 1.0, stop_checker=lambda: True)
    assert isinstance(res, FailedAfterStart)
    assert res.kind == 'cancel'
    with pytest.raises(IpcPartialFrameTimeout):
        mgr._read_frame_bytes(_Stdout(), 1.0, stop_checker=lambda: False)

def test_manager_separate_pools_same_exe():
    from plugin.framework.constants import WORKER_POOL_DEFAULT, WORKER_POOL_EMBEDDINGS
    shutdown_venv_workers_for_tests()
    env = {'PATH': '/usr/bin:/bin'}
    default_mgr = PythonWorkerManager.get(sys.executable, env, pool=WORKER_POOL_DEFAULT)
    embed_mgr = PythonWorkerManager.get(sys.executable, env, pool=WORKER_POOL_EMBEDDINGS)
    assert default_mgr is not embed_mgr
    assert default_mgr is PythonWorkerManager.get(sys.executable, env, pool=WORKER_POOL_DEFAULT)
    shutdown_venv_workers_for_tests()

def test_split_grid_data_round_trip_execute_request():
    """Ingress split_grid: child receives CalcRange backed by numeric values."""
    pytest.importorskip('numpy')
    from plugin.calc.calc_addin_data import pack_calc_data_for_wire
    from plugin.scripting.payload_codec import BINARY_MIN_CELLS, is_split_grid
    from plugin.scripting.calc_range import is_calc_range_payload
    from tests.scripting.payload_codec_test_support import NUMERIC_AT_THRESHOLD, sequential_grid_sum
    grid = NUMERIC_AT_THRESHOLD
    wire = pack_calc_data_for_wire(grid)
    assert is_calc_range_payload(wire)
    assert is_split_grid(wire['data'])
    r = run_sandboxed_code('result = float(np.sum(data))', wire)
    assert r['status'] == 'ok'
    assert r['result'] == pytest.approx(sequential_grid_sum(BINARY_MIN_CELLS))

def test_normalize_response_unpacks_split_grid():
    from plugin.scripting.payload_codec import host_pack_split_grid, is_split_grid
    grid = [[float(r * 10 + c) for c in range(5)] for r in range(5)]
    wire = host_pack_split_grid(grid)
    assert is_split_grid(wire)
    mgr = PythonWorkerManager(sys.executable, {'PATH': '/usr/bin:/bin'})
    out = mgr._normalize_response({'status': 'ok', 'result': wire, 'stdout': ''})
    assert out['status'] == 'ok'
    assert not is_split_grid(out['result'])
    assert len(out['result']) == 5
    assert out['result'][0][0] == pytest.approx(0.0)
    assert out['result'][4][4] == pytest.approx(44.0)

@pytest.mark.parametrize('value, expected', [pytest.param('result = math.sqrt(16)', 4.0, id='test_automatic_imports_math'), pytest.param('import math as my_math\nresult = my_math.sqrt(16)', 4.0, id='test_automatic_imports_already_imported'), pytest.param('import math\nresult = math.sqrt(25)', 5.0, id='test_automatic_imports_explicit')])
def test_automatic_imports_math(value, expected):
    r = run_sandboxed_code(value, None)
    assert r['status'] == 'ok'
    assert r['result'] == expected

@pytest.mark.parametrize('value, value_2, expected', [pytest.param('numpy', 'result = float(np.sum([1, 2, 3]))', 6.0, id='test_automatic_imports_numpy'), pytest.param('sympy', "result = str(sp.Symbol('x'))", 'x', id='test_automatic_imports_sympy')])
def test_automatic_imports_numpy(value, value_2, expected):
    pytest.importorskip(value)
    r = run_sandboxed_code(value_2, None)
    assert r['status'] == 'ok'
    assert r['result'] == expected

def test_build_request_sets_heartbeat_on_trusted_action():
    req = _ExecRequest(action='run_trusted_action', allow_heartbeat=True, data={'domain': 'embeddings_index'})
    request = req.wire()
    assert request == req.wire()
    assert request['id'] == req.request_id
    assert request['allow_heartbeat'] is True
    assert request['action'] == 'run_trusted_action'
    assert request['data'] is req.data
    assert 'code' not in request
    assert 'timeout_sec' not in request
    assert 'cancellation_scope' not in request
    assert 'script_session_id' not in request
    empty = _ExecRequest(session=None, allow_heartbeat=False, bindings={}, code=None).wire()
    assert empty['code'] == ''
    assert 'session_id' not in empty
    assert 'allow_heartbeat' not in empty
    assert 'bindings' not in empty

def test_exec_request_wire_sandbox_policy():
    policy = {'network': True, 'files': True, 'os': False, 'extra_modules': 'requests'}
    code_req = _ExecRequest(code='x = 1', sandbox_policy=policy)
    code_wire = code_req.wire()
    assert code_wire['sandbox_policy'] == policy
    assert code_wire['code'] == 'x = 1'
    reset_req = _ExecRequest(action='reset_session', sandbox_policy=policy)
    reset_wire = reset_req.wire()
    assert 'sandbox_policy' not in reset_wire
    assert reset_wire['action'] == 'reset_session'
    trusted_req = _ExecRequest(action='run_trusted_action', data={'domain': 'analysis'}, sandbox_policy=policy)
    trusted_wire = trusted_req.wire()
    assert 'sandbox_policy' not in trusted_wire
    assert trusted_wire['action'] == 'run_trusted_action'

def test_run_code_in_user_venv_reads_sandbox_policy_flags():
    with patch('plugin.scripting.venv_worker.configured_python_exec_timeout', return_value=10), patch('plugin.scripting.venv_worker.resolve_libreoffice_python', return_value=sys.executable), patch('plugin.scripting.venv_worker.PythonWorkerManager.execute') as mock_execute:
        mock_execute.return_value = {'status': 'ok', 'result': 1}

        def fake_get_config(key: str):
            if key == 'scripting.python_allow_os':
                return True
            if key in ('scripting.python_allow_network', 'scripting.python_allow_files'):
                return False
            if key == 'scripting.python_extra_modules':
                return 'custom_mod'
            return ''
        with patch('plugin.scripting.venv_worker.get_config', side_effect=fake_get_config):
            run_code_in_user_venv('result = 1')
            assert mock_execute.call_args.kwargs['sandbox_policy'] == {'network': False, 'files': True, 'os': True, 'extra_modules': 'custom_mod'}
        mock_execute.reset_mock()

        def fake_get_config_net_files(key: str):
            if key in ('scripting.python_allow_network', 'scripting.python_allow_files'):
                return True
            if key == 'scripting.python_extra_modules':
                return ''
            return False
        with patch('plugin.scripting.venv_worker.get_config', side_effect=fake_get_config_net_files):
            run_code_in_user_venv('result = 1')
            assert mock_execute.call_args.kwargs['sandbox_policy'] == {'network': True, 'files': True, 'os': False, 'extra_modules': ''}

@pytest.mark.parametrize('exc_type', [ValueError, OverflowError, TypeError])
def test_bad_result_after_tool_call_does_not_replay(monkeypatch, exc_type):
    """Unpack failures after a finished tool call must not resend the script.

    ValueError was already caught. OverflowError (int(inf) on an int column)
    and TypeError are on the same host-unpack contract and used to escape.
    """
    import plugin.scripting.venv_worker as venv_worker_module
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    _install_mock_proc(mgr, proc)
    mgr._ensure_running = MagicMock()
    captured: dict[str, dict] = {}

    def _write_frame(stdin, request, **kwargs):
        captured['request'] = request
    mgr._write_frame_with_timeout = _write_frame
    reads = {'n': 0}

    def _read(stdout, timeout_sec, stop_checker=None):
        reads['n'] += 1
        if reads['n'] == 1:
            return pickle.dumps({'status': 'host_request'}, protocol=5)
        request = captured['request']
        return pickle.dumps({'status': 'ok', 'id': request['id'], 'result': {'split_grid': 'bad'}}, protocol=5)
    mgr._read_response_bytes = _read
    mgr._terminate_worker = MagicMock()

    def _normalize(response):
        raise exc_type('inconsistent split_grid')
    mgr._normalize_response = _normalize
    dispatched = {'n': 0}

    def dispatch(response, *, stdin_write, on_worker_event=None, stop_checker=None, **kwargs):
        del response, stdin_write, on_worker_event, stop_checker, kwargs
        dispatched['n'] += 1
        return dispatched['n'] == 1
    monkeypatch.setattr(venv_worker_module, '_maybe_dispatch_intermediate_response', dispatch)
    result = _run_ipc_with_mock(mgr, _ExecRequest('result = 1', timeout_sec=1), proc)
    assert result['status'] == 'error'
    assert result['code'] == 'WORKER_IPC_ERROR'
    assert 'inconsistent split_grid' in result['message']
    assert 'request' in captured
    assert reads['n'] == 2
    assert mgr._terminate_worker.call_count == 1

def test_get_replaces_previous_interpreter_in_the_same_pool():
    from plugin.framework.constants import WORKER_POOL_DEFAULT
    from plugin.scripting import venv_worker
    shutdown_venv_workers_for_tests()
    first = PythonWorkerManager.get('/tmp/python-a', {'PATH': '/usr/bin'})
    second = PythonWorkerManager.get('/tmp/python-b', {'PATH': '/usr/bin'}, pool=WORKER_POOL_DEFAULT)
    try:
        assert first is not second
        assert PythonWorkerManager.get('/tmp/python-b', {'PATH': '/usr/bin'}) is second
        assert all((not key.endswith('python-a') for key in venv_worker._instances))
    finally:
        shutdown_venv_workers_for_tests()

def test_get_refreshes_env_for_the_next_spawn():
    shutdown_venv_workers_for_tests()
    try:
        first = PythonWorkerManager.get('/tmp/python-env', {'PATH': '/usr/bin', 'A': '1'})
        second = PythonWorkerManager.get('/tmp/python-env', {'PATH': '/usr/bin', 'A': '1', 'B': '2'})
        assert first is second
        assert second.env['B'] == '2'
        assert second.env['WRITERAGENT_IS_WORKER'] == '1'
    finally:
        shutdown_venv_workers_for_tests()

def test_retired_worker_does_not_respawn():
    from plugin.framework.constants import WORKER_POOL_DEFAULT
    shutdown_venv_workers_for_tests()
    try:
        first = PythonWorkerManager.get('/tmp/python-old-path', {'PATH': '/usr/bin'}, pool=WORKER_POOL_DEFAULT)
        mock_warm = MagicMock()
        mock_warm.is_alive.return_value = True
        first._warm = mock_warm
        PythonWorkerManager.get('/tmp/python-new-path', {'PATH': '/usr/bin'}, pool=WORKER_POOL_DEFAULT)
        assert first._retired is True
        mock_warm.retire.assert_called_once()
        assert first._ensure_running() is False
        assert _mgr_proc(first) is None
    finally:
        shutdown_venv_workers_for_tests()

def test_shutdown_all_sets_retired_before_terminate():
    from plugin.scripting import venv_worker as vw
    mgr = vw.PythonWorkerManager.__new__(vw.PythonWorkerManager)
    mgr._retired = False
    mgr._warm = MagicMock()
    mgr._warm.retire = MagicMock()
    key = 'test-pool:shutdown-retire'
    with vw._registry_lock:
        previous = dict(vw._instances)
        vw._instances[key] = mgr
    try:
        vw.shutdown_venv_workers_for_tests()
    finally:
        with vw._registry_lock:
            vw._instances.clear()
            vw._instances.update(previous)
    assert mgr._retired is True

@patch('plugin.scripting.venv_worker.configured_python_exec_timeout', return_value=10)
@patch('plugin.scripting.venv_worker.get_config', return_value='')
@patch('plugin.scripting.venv_worker.resolve_libreoffice_python', return_value=sys.executable)
@patch('plugin.scripting.venv_worker.PythonWorkerManager.execute')
def test_run_venv_code_timeout_capped(mock_execute, mock_lo_python, mock_cfg, mock_configured_timeout):
    default_policy = {'network': False, 'files': False, 'os': False, 'extra_modules': ''}
    run_code_in_user_venv('result = 1')
    mock_execute.assert_called_once_with('result = 1', data=None, bindings=None, timeout_sec=10, session_id=None, init_script=None, init_session_id=None, init_script_hash=None, allow_heartbeat=False, on_heartbeat=None, action=None, python_tool_domain=None, script_session_id=None, stop_checker=None, cancellation_scope=None, sandbox_policy=default_policy)
    mock_execute.reset_mock()
    run_code_in_user_venv('result = 1', timeout_sec=100)
    mock_execute.assert_called_once_with('result = 1', data=None, bindings=None, timeout_sec=100, session_id=None, init_script=None, init_session_id=None, init_script_hash=None, allow_heartbeat=False, on_heartbeat=None, action=None, python_tool_domain=None, script_session_id=None, stop_checker=None, cancellation_scope=None, sandbox_policy=default_policy)
    mock_execute.reset_mock()
    run_code_in_user_venv('result = 1', timeout_sec=1000)
    mock_execute.assert_called_once_with('result = 1', data=None, bindings=None, timeout_sec=600, session_id=None, init_script=None, init_session_id=None, init_script_hash=None, allow_heartbeat=False, on_heartbeat=None, action=None, python_tool_domain=None, script_session_id=None, stop_checker=None, cancellation_scope=None, sandbox_policy=default_policy)
    mock_execute.reset_mock()
    run_code_in_user_venv('result = 1', timeout_sec=0)
    mock_execute.assert_called_once_with('result = 1', data=None, bindings=None, timeout_sec=1, session_id=None, init_script=None, init_session_id=None, init_script_hash=None, allow_heartbeat=False, on_heartbeat=None, action=None, python_tool_domain=None, script_session_id=None, stop_checker=None, cancellation_scope=None, sandbox_policy=default_policy)

def test_run_code_forwards_script_session_pin_apart_from_kernel_session():
    with patch('plugin.scripting.venv_worker.configured_python_exec_timeout', return_value=10), patch('plugin.scripting.venv_worker.get_config', return_value=''), patch('plugin.scripting.venv_worker.resolve_libreoffice_python', return_value=sys.executable), patch('plugin.scripting.venv_worker.PythonWorkerManager.execute') as mock_execute:
        mock_execute.return_value = {'status': 'ok', 'result': 1}
        run_code_in_user_venv('result = 1', script_session_id='doc:pin')
    assert mock_execute.call_args.kwargs['script_session_id'] == 'doc:pin'
    assert mock_execute.call_args.kwargs['session_id'] is None

def test_host_script_session_id_prefers_pin():
    from plugin.scripting.venv_worker import host_script_session_id
    assert host_script_session_id('rps:file:///a.odg', 'doc:pin') == 'doc:pin'
    assert host_script_session_id('rps:file:///a.odg', None) == 'rps:file:///a.odg'
    assert host_script_session_id('rps:file:///a.odg', '  ') == 'rps:file:///a.odg'
    assert host_script_session_id(None, None) is None
    assert host_script_session_id('  ', 'doc:pin') == 'doc:pin'

def _ipc_one_tool_call(*, session_id, script_session_id):
    """Drive one tool_call frame through the host without a real worker."""
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    _install_mock_proc(mgr, proc)
    mgr._ensure_running = MagicMock()
    captured: dict[str, dict] = {}

    def _write_frame(stdin, request, **kwargs):
        del stdin, kwargs
        captured['request'] = request
    mgr._write_frame_with_timeout = _write_frame
    reads = {'n': 0}

    def _read(stdout, timeout_sec, stop_checker=None):
        del stdout, timeout_sec
        reads['n'] += 1
        request = captured['request']
        if reads['n'] == 1:
            return pickle.dumps({'type': 'tool_call', 'id': 'c1', 'tool': 'shape_upsert', 'args': {'action': 'create'}}, protocol=5)
        return pickle.dumps({'status': 'ok', 'id': request['id'], 'result': 1}, protocol=5)
    mgr._read_response_bytes = _read
    with patch('plugin.scripting.host_rpc.execute_tool', return_value={'status': 'ok'}) as mock_tool:
        result = _run_ipc_with_mock(
            mgr,
            _ExecRequest('result = 1', timeout_sec=1, session=SessionKey.from_host(session_id), pin=script_session_id),
            proc,
        )
    return (captured['request'], mock_tool.call_args, result)

def test_pinned_script_session_reaches_tool_rpc_without_kernel_session():
    request, call, result = _ipc_one_tool_call(session_id=None, script_session_id='doc:pinned-deck')
    assert result['status'] == 'ok'
    assert 'session_id' not in request
    assert call.kwargs['script_session_id'] == 'doc:pinned-deck'

def test_request_session_id_still_reaches_tool_rpc_without_pin():
    request, call, result = _ipc_one_tool_call(session_id='rps:file:///deck.odg', script_session_id=None)
    assert result['status'] == 'ok'
    assert request['session_id'] == 'rps:file:///deck.odg'
    assert call.kwargs['script_session_id'] == 'rps:file:///deck.odg'

def test_execute_forwards_script_session_id_not_as_kernel_session():
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock(stdin=io.BytesIO(), stdout=io.BytesIO())
    leased = _lease_from_proc(mgr, proc)

    def _run(body, **kwargs):
        return body(leased)

    with patch.object(mgr, '_run_in_session', side_effect=_run), patch.object(mgr, '_execute_ipc_unlocked', return_value={'status': 'ok'}) as ipc:
        mgr.execute('result = 1', script_session_id='doc:pin')
    assert ipc.call_args.kwargs.get('leased') is leased
    req = ipc.call_args.args[0]
    assert req.pin == 'doc:pin'
    assert req.session is None
    assert 'session_id' not in req.wire()
    assert 'script_session_id' not in req.wire()

def test_split_grid_pickle_and_json_round_trip():
    """Regression: production buffer path vs historical Base64 JSON split_grid."""
    from plugin.scripting.payload_codec import is_split_grid
    from tests.scripting.payload_codec_test_support import child_pack_split_grid, child_unpack_split_grid, host_pack_split_grid, host_unpack_split_grid, legacy_b64_child_pack_split_grid, legacy_b64_child_unpack_split_grid, legacy_b64_host_pack_split_grid, legacy_b64_host_unpack_split_grid
    np = pytest.importorskip('numpy')
    grid = [[float(r * 10 + c) for c in range(4)] for r in range(4)]
    wire_json = legacy_b64_host_pack_split_grid(grid)
    assert is_split_grid(wire_json)
    assert 'b64' in wire_json
    assert 'buffer' not in wire_json
    assert isinstance(wire_json['b64'], str)
    unpacked_host_json = legacy_b64_host_unpack_split_grid(wire_json)
    assert unpacked_host_json == grid
    unpacked_child_json = legacy_b64_child_unpack_split_grid(wire_json)
    assert isinstance(unpacked_child_json, np.ndarray)
    assert unpacked_child_json.shape == (4, 4)
    np.testing.assert_allclose(unpacked_child_json, np.array(grid))
    wire_pickle = host_pack_split_grid(grid)
    assert is_split_grid(wire_pickle)
    assert 'buffer' in wire_pickle
    assert 'b64' not in wire_pickle
    assert isinstance(wire_pickle['buffer'], bytes)
    unpacked_host_pickle = host_unpack_split_grid(wire_pickle)
    assert unpacked_host_pickle == grid
    unpacked_child_pickle = child_unpack_split_grid(wire_pickle)
    assert isinstance(unpacked_child_pickle, np.ndarray)
    assert unpacked_child_pickle.shape == (4, 4)
    np.testing.assert_allclose(unpacked_child_pickle, np.array(grid))
    child_wire_json = legacy_b64_child_pack_split_grid(np.array(grid))
    assert is_split_grid(child_wire_json)
    assert 'b64' in child_wire_json
    assert 'buffer' not in child_wire_json
    unpacked_host_json_from_child = legacy_b64_host_unpack_split_grid(child_wire_json)
    assert unpacked_host_json_from_child == grid
    child_wire_pickle = child_pack_split_grid(np.array(grid))
    assert is_split_grid(child_wire_pickle)
    assert 'buffer' in child_wire_pickle
    assert 'b64' not in child_wire_pickle
    unpacked_host_pickle_from_child = host_unpack_split_grid(child_wire_pickle)
    assert unpacked_host_pickle_from_child == grid

def test_warm_logs_prime_failure(caplog: pytest.LogCaptureFixture) -> None:
    mgr = PythonWorkerManager('/tmp/python-warm-fail', {'PATH': '/usr/bin'})
    mgr._ensure_warmed = lambda: {'status': 'error', 'message': 'prime failed'}
    with caplog.at_level(logging.WARNING, logger='plugin.scripting.venv_worker'):
        mgr.warm()
    assert 'prime failed' in caplog.text

def test_retired_execute_does_not_clear_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """A replaced manager must not kill a worker or wipe the Calc scalar cache."""
    mgr = PythonWorkerManager('/tmp/python-retired', {'PATH': '/usr/bin'})
    mgr._retired = True
    terminated: list[bool] = []
    cleared: list[bool] = []
    monkeypatch.setattr(mgr, '_terminate_worker', lambda: terminated.append(True))

    def _clear() -> None:
        cleared.append(True)
    monkeypatch.setattr('plugin.calc.python.function.clear_python_addin_cache', _clear)
    result = mgr.execute('result = 1', timeout_sec=5)
    assert result['status'] == 'error'
    assert result['code'] == 'WORKER_IPC_ERROR'
    assert 'will not be restarted' in result['message']
    assert terminated == []
    assert cleared == []

def test_warm_spawns_and_primes_worker():
    """warm() makes the next execute instant by pre-spawning the process and triggering auto-imports."""
    shutdown_venv_workers_for_tests()
    mgr = PythonWorkerManager.get(sys.executable, {'PATH': '/usr/bin:/bin'})
    assert _mgr_proc(mgr) is None
    mgr.warm()
    assert _mgr_proc(mgr) is not None and _mgr_proc(mgr).poll() is None
    assert mgr._primed is True
    r = mgr.execute('result = 42')
    assert r['status'] == 'ok'
    assert r['result'] == 42
    shutdown_venv_workers_for_tests()

def test_cold_execute_warms_with_separate_timeout():
    """First execute primes the worker under WARM_WORKER_TIMEOUT_SEC, then runs user code at configured timeout."""
    from plugin.scripting.config_limits import HOST_IPC_READ_GRACE_SEC
    shutdown_venv_workers_for_tests()
    mgr = PythonWorkerManager.get(sys.executable, {'PATH': '/usr/bin:/bin'})
    timeouts: list[float | int] = []
    original_read = mgr._read_response_bytes

    def record_read(stdout, timeout_sec, stop_checker=None):
        timeouts.append(timeout_sec)
        return original_read(stdout, timeout_sec)
    mgr._read_response_bytes = record_read
    try:
        r = mgr.execute('result = 42', timeout_sec=3)
        assert r['status'] == 'ok'
        assert r['result'] == 42
        grace = HOST_IPC_READ_GRACE_SEC
        assert timeouts == [WARM_WORKER_TIMEOUT_SEC + grace, WARM_WORKER_TIMEOUT_SEC + grace, 3 + grace, 3 + grace]
    finally:
        shutdown_venv_workers_for_tests()

def test_warm_execute_uses_configured_timeout_only():
    """After priming, execute reads exec_started and the result at the configured timeout."""
    from plugin.scripting.config_limits import HOST_IPC_READ_GRACE_SEC
    shutdown_venv_workers_for_tests()
    mgr = PythonWorkerManager.get(sys.executable, {'PATH': '/usr/bin:/bin'})
    mgr.warm()
    timeouts: list[float | int] = []
    original_read = mgr._read_response_bytes

    def record_read(stdout, timeout_sec, stop_checker=None):
        timeouts.append(timeout_sec)
        return original_read(stdout, timeout_sec)
    mgr._read_response_bytes = record_read
    try:
        r = mgr.execute('result = 7', timeout_sec=3)
        assert r['status'] == 'ok'
        assert r['result'] == 7
        grace = HOST_IPC_READ_GRACE_SEC
        assert timeouts == [3 + grace, 3 + grace]
    finally:
        shutdown_venv_workers_for_tests()

def test_terminate_worker_re_primes_on_next_execute():
    """After worker kill, the next execute runs warm again before user code."""
    from plugin.scripting.config_limits import HOST_IPC_READ_GRACE_SEC
    shutdown_venv_workers_for_tests()
    mgr = PythonWorkerManager.get(sys.executable, {'PATH': '/usr/bin:/bin'})
    mgr.warm()
    mgr._terminate_worker()
    timeouts: list[float | int] = []
    original_read = mgr._read_response_bytes

    def record_read(stdout, timeout_sec, stop_checker=None):
        timeouts.append(timeout_sec)
        return original_read(stdout, timeout_sec)
    mgr._read_response_bytes = record_read
    try:
        r = mgr.execute('result = 99', timeout_sec=3)
        assert r['status'] == 'ok'
        assert r['result'] == 99
        grace = HOST_IPC_READ_GRACE_SEC
        assert timeouts == [WARM_WORKER_TIMEOUT_SEC + grace, WARM_WORKER_TIMEOUT_SEC + grace, 3 + grace, 3 + grace]
    finally:
        shutdown_venv_workers_for_tests()

@patch('plugin.scripting.venv_worker.get_config', return_value='')
@patch('plugin.scripting.venv_worker.resolve_libreoffice_python', return_value=sys.executable)
def test_warm_venv_worker_resolves_and_warms(mock_lo_python, mock_cfg):
    shutdown_venv_workers_for_tests()
    warm_venv_worker()
    mgr = PythonWorkerManager.get(sys.executable, scrub_subprocess_env({'PATH': '/usr/bin:/bin'}))
    assert _mgr_proc(mgr) is not None and _mgr_proc(mgr).poll() is None
    shutdown_venv_workers_for_tests()

class TestLiveWorkerReuse:
    """Contiguous live-worker tests sharing one warmed manager (after lifecycle tests above)."""

    @pytest.fixture(scope='class')
    @classmethod
    def warmed_worker_manager(cls):
        shutdown_venv_workers_for_tests()
        mgr = PythonWorkerManager.get(sys.executable, {'PATH': '/usr/bin:/bin'})
        mgr.warm()
        yield mgr
        shutdown_venv_workers_for_tests()

    @patch('plugin.scripting.venv_worker.get_config', return_value='')
    @patch('plugin.scripting.venv_worker.resolve_libreoffice_python', return_value=sys.executable)
    def test_run_code_uses_manager(self, mock_lo_python, mock_cfg, warmed_worker_manager):
        del warmed_worker_manager
        r1 = run_code_in_user_venv('result = 100')
        assert r1['status'] == 'ok'
        assert r1['result'] == 100
        r2 = run_code_in_user_venv('result = nope + 1')
        assert r2['status'] == 'error'

    def test_manager_two_calls_same_process(self, warmed_worker_manager):
        mgr = warmed_worker_manager
        r1 = mgr.execute('result = 1')
        assert r1['status'] == 'ok'
        pid1 = _mgr_proc(mgr).pid if _mgr_proc(mgr) else None
        r2 = mgr.execute('result = 2')
        assert r2['status'] == 'ok'
        pid2 = _mgr_proc(mgr).pid if _mgr_proc(mgr) else None
        assert pid1 is not None and pid1 == pid2
        r3 = mgr.execute('result = prev')
        assert r3['status'] == 'error'

    def test_split_grid_result_round_trip_manager(self, warmed_worker_manager):
        """API responses unpack split_grid so LLM/UI never see wire envelopes."""
        pytest.importorskip('numpy')
        mgr = warmed_worker_manager
        r = mgr.execute('import numpy as np\nresult = np.arange(16, dtype=np.float64).reshape(4, 4)')
        assert r['status'] == 'ok'
        from plugin.scripting.payload_codec import is_split_grid
        assert not is_split_grid(r['result'])
        assert len(r['result']) == 4
        assert r['result'][0][0] == pytest.approx(0.0)
        assert r['result'][3][3] == pytest.approx(15.0)

    def test_manager_unpacks_prime_tuple_list(self, warmed_worker_manager):
        """List-of-tuples large enough for split_grid on wire must return nested lists to callers."""
        pytest.importorskip('sympy')
        mgr = warmed_worker_manager
        code = 'result = [(i, int(sp.prime(i))) for i in range(100, 107)]'
        r = mgr.execute(code)
        assert r['status'] == 'ok'
        from plugin.scripting.payload_codec import is_split_grid
        assert not is_split_grid(r['result'])
        assert r['result'] == [[100, 541], [101, 547], [102, 557], [103, 563], [104, 569], [105, 571], [106, 577]]
        assert all((isinstance(cell, int) for row in r['result'] for cell in row))

    def test_split_grid_integration_pickle_mode(self, warmed_worker_manager):
        pytest.importorskip('numpy')
        mgr = warmed_worker_manager
        r = mgr.execute('import numpy as np\nresult = np.arange(100, dtype=np.float64).reshape(10, 10)')
        assert r['status'] == 'ok'
        assert len(r['result']) == 10
        assert r['result'][0][0] == 0.0
        assert r['result'][9][9] == 99.0
        large_grid = [[float(r * 10 + c) for c in range(10)] for r in range(10)]
        from plugin.calc.calc_addin_data import pack_calc_data_for_wire
        r2 = mgr.execute('result = float(np.sum(data))', data=pack_calc_data_for_wire(large_grid))
        assert r2['status'] == 'ok'
        assert r2['result'] == pytest.approx(sum((r * 10 + c for r in range(10) for c in range(10))))

def _pack_response(obj: dict) -> bytes:
    """Encode a response the same way worker_harness.py does."""
    return pack_pickle_frame(obj)

class TestReadResponseBytesSelect:
    """Tests for reading a valid response from an unbuffered pipe."""

    def test_reads_valid_response(self):
        response = {'status': 'ok', 'result': 'hello', 'id': 'test'}
        raw = _pack_response(response)
        r_fd, w_fd = os.pipe()
        os.write(w_fd, raw)
        os.close(w_fd)
        stdout = os.fdopen(r_fd, 'rb', buffering=0)
        mgr = PythonWorkerManager.__new__(PythonWorkerManager)
        mgr.exe = 'python'
        _install_mock_proc(mgr, MagicMock(poll=MagicMock(return_value=None)))
        try:
            got = mgr._read_response_bytes(stdout, timeout_sec=5)
        finally:
            stdout.close()
        assert got
        decoded = pickle.loads(got)
        assert decoded['result'] == 'hello'

def test_io_session_body_oserror_does_not_disable_systemd_scope():
    """OSError after lease acquisition must propagate; it is not a systemd-run spawn failure."""
    import contextlib
    from plugin.framework.warm_child import Lease

    mgr = PythonWorkerManager(sys.executable, {'PATH': os.environ.get('PATH', '')})
    warm = MagicMock()
    warm._io_lock = threading.Lock()

    @contextlib.contextmanager
    def fake_lease(*args: object, **kwargs: object):
        del args, kwargs
        proc = MagicMock()
        proc.stdin = io.BytesIO()
        proc.stdout = io.BytesIO()
        yield Lease(stdin=proc.stdin, stdout=proc.stdout, pid=1, generation=1, warm=warm)

    warm.lease = fake_lease
    mgr._warm = warm
    orig_disabled = venv_worker_module._systemd_scope_disabled
    try:
        venv_worker_module._systemd_scope_disabled = False
        with patch.object(mgr, '_ensure_warmed_unlocked', return_value=None):
            with pytest.raises(OSError, match='session body'):
                with mgr._io_session():
                    raise OSError('session body')
        assert venv_worker_module._systemd_scope_disabled is False
    finally:
        venv_worker_module._systemd_scope_disabled = orig_disabled

class TestExecuteOSErrorRetry:
    """Verify that OSError on the initial write triggers one lease retry."""

    def test_oserror_retried(self):
        mgr = PythonWorkerManager(sys.executable, {'PATH': os.environ.get('PATH', '')})
        proc = MagicMock()
        proc.poll.return_value = None
        proc.stdin = io.BytesIO()
        proc.stdout = io.BytesIO()
        _install_mock_proc(mgr, proc)
        calls = [0]

        def fake_write(*args: object, **kwargs: object) -> None:
            del args, kwargs
            calls[0] += 1
            raise OSError('[WinError 10038] not a socket')

        mgr._write_frame_with_timeout = fake_write  # type: ignore[method-assign]
        mgr._terminate_worker = MagicMock()  # type: ignore[method-assign]
        result = _run_ipc_with_mock(mgr, _ExecRequest('result = 1', timeout_sec=1), proc)
        assert result['status'] == 'error'
        assert '10038' in result['message']
        assert calls[0] == 2
        assert mgr._terminate_worker.call_count == 2

def test_maybe_dispatch_ppt_master_skips_when_module_missing(monkeypatch):
    import builtins
    from plugin.scripting.venv_worker import _maybe_dispatch_intermediate_response
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'plugin.ppt_master.venv.host_rpc' or (name == 'plugin.ppt_master' and fromlist and ('venv' in fromlist)):
            raise ModuleNotFoundError(f'No module named {name!r}', name=name)
        return real_import(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, '__import__', fake_import)
    assert _maybe_dispatch_intermediate_response({'status': 'ok', 'result': 2}, stdin_write=MagicMock(), caller='ppt_master_venv') is False

def test_shared_session_persists_after_soft_timeout():
    """Soft in-process timeout must return an error without terminating the worker or shared session."""
    import plugin.scripting.venv_worker as vw
    mgr = PythonWorkerManager(sys.executable, {'PATH': os.environ.get('PATH', '')})
    sid = 'test-shared-timeout-session'
    try:
        r1 = mgr.execute('x = 12345\nresult = x', session_id=sid)
        assert r1['status'] == 'ok'
        assert r1['result'] == 12345
        with patch.object(vw, 'HOST_IPC_READ_GRACE_SEC', 8.0):
            r2 = mgr.execute('import time\ntime.sleep(5)', session_id=sid, timeout_sec=1)
        assert r2['status'] == 'error'
        assert 'execution time' in r2.get('message', '').lower() or 'timed out' in r2.get('message', '').lower()
        r3 = mgr.execute('result = x + 1', session_id=sid)
        assert r3['status'] == 'ok'
        assert r3['result'] == 12346
    finally:
        mgr._terminate_worker()

def test_init_and_cell_share_one_timeout_without_killing_worker():
    """Init and the cell used to each get a full alarm, so the host read killed the worker."""
    import plugin.scripting.venv_worker as vw
    mgr = PythonWorkerManager(sys.executable, {'PATH': os.environ.get('PATH', '')})
    sid = 'test-shared-budget-session'
    try:
        with patch.object(vw, 'HOST_IPC_READ_GRACE_SEC', 8.0):
            slow = mgr.execute('import time\ntime.sleep(2)\nresult = 1', session_id=sid, timeout_sec=3, init_script='import time\ntime.sleep(2)\n', init_session_id=sid + ':init', init_script_hash='budget-v1')
        assert slow['status'] == 'error'
        assert 'execution time' in slow.get('message', '').lower() or 'timed out' in slow.get('message', '').lower()
        assert _mgr_proc(mgr) is not None and _mgr_proc(mgr).poll() is None
        follow = mgr.execute('result = 7', session_id=sid)
        assert follow['status'] == 'ok'
        assert follow['result'] == 7
    finally:
        mgr._terminate_worker()

def test_session_executor_updates_timeout_seconds():
    """_get_or_create_session_executor updates timeout_seconds on an existing session."""
    from plugin.scripting.venv.venv_sandbox import _get_or_create_session_executor, reset_sandbox_session
    sid = 'test-dynamic-timeout-session'
    try:
        exec1, created1 = _get_or_create_session_executor(sid, timeout_sec=10)
        assert created1 is True
        assert exec1.timeout_seconds == 10
        exec2, created2 = _get_or_create_session_executor(sid, timeout_sec=3)
        assert created2 is False
        assert exec2 is exec1
        assert exec2.timeout_seconds == 3
    finally:
        reset_sandbox_session(sid)

def test_venv_worker_error_codes_and_context():
    """Verify venv worker returns structured error codes and context on failure."""
    mgr = PythonWorkerManager(sys.executable, {'PATH': os.environ.get('PATH', '')})
    try:
        res = mgr.execute('1 / 0')
        assert res['status'] == 'error'
        assert res.get('code') == 'VENV_EXEC_ERROR'
        assert 'ZeroDivisionError' in res.get('message', '')
        assert 'traceback' in res
        res_to = mgr.execute('import time\ntime.sleep(5)', timeout_sec=1)
        assert res_to['status'] == 'error'
        assert res_to.get('code') in ('VENV_TIMEOUT', 'VENV_EXEC_ERROR')
    finally:
        mgr._terminate_worker()

def test_worker_error_shape():
    err = _worker_error('WORKER_IPC_ERROR', 'No code provided.')
    assert err == {'status': 'error', 'code': 'WORKER_IPC_ERROR', 'message': 'No code provided.', 'details': {}}

def test_run_code_and_reset_session_missing_inputs_include_code():
    empty_code = run_code_in_user_venv('   ')
    assert empty_code['status'] == 'error'
    assert empty_code['code'] == 'WORKER_IPC_ERROR'
    assert empty_code['message'] == 'No code provided.'
    assert 'details' in empty_code
    empty_sid = reset_python_session('  ')
    assert empty_sid['status'] == 'error'
    assert empty_sid['code'] == 'WORKER_IPC_ERROR'
    assert empty_sid['message'] == 'No session_id provided.'
    assert 'details' in empty_sid

def test_reset_python_session_does_not_spawn_when_worker_is_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    """OnUnload calls reset on the UNO thread. A dead child must not execute().

    execute() warms via _ensure_running -> _start_child, and the caller blocks
    on venv-worker-spawn. Before the guard this records one reset_session call.
    """
    from plugin.scripting import venv_worker as venv_worker_module
    calls: list[str | None] = []

    def _execute(self: PythonWorkerManager, *args: object, **kwargs: object) -> dict[str, object]:
        del self, args
        action = kwargs.get('action')
        calls.append(action if isinstance(action, str) else None)
        return {'status': 'ok'}
    monkeypatch.setattr(PythonWorkerManager, 'execute', _execute)
    dead = PythonWorkerManager('/tmp/python-dead', {'PATH': '/usr/bin'})
    assert dead._is_worker_alive() is False
    monkeypatch.setattr(venv_worker_module, '_get_worker_manager', lambda pool='default': (dead, None))
    res = reset_python_session('calc:file:///closed.ods')
    assert res['status'] == 'ok'
    assert calls == []
    alive = PythonWorkerManager('/tmp/python-alive', {'PATH': '/usr/bin'})
    _install_mock_proc(alive, _MockProc(None))
    monkeypatch.setattr(venv_worker_module, '_get_worker_manager', lambda pool='default': (alive, None))
    res_live = reset_python_session('calc:file:///live.ods')
    assert res_live['status'] == 'ok'
    assert calls == ['reset_session']

def test_worker_read_rejects_oversize_length_prefix():
    mgr = PythonWorkerManager(sys.executable, {'PATH': os.environ.get('PATH', '')})
    _install_mock_proc(mgr, MagicMock(poll=MagicMock(return_value=None)))
    too_big = struct.pack('!I', DEFAULT_MAX_PAYLOAD_BYTES + 1)
    with pytest.raises(IpcFrameError, match='venv worker frame'):
        mgr._read_frame_bytes(io.BytesIO(too_big), timeout_sec=2)

def test_maybe_dispatch_tool_call_without_ppt_master(monkeypatch):
    """tool_call must round-trip even when ppt-master is not bundled."""
    import builtins
    from plugin.scripting.venv_worker import _maybe_dispatch_intermediate_response
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'plugin.ppt_master.venv.host_rpc' or (name == 'plugin.ppt_master' and fromlist and ('venv' in fromlist)):
            raise ModuleNotFoundError(f'No module named {name!r}', name=name)
        return real_import(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, '__import__', fake_import)
    written: list[bytes] = []
    with patch('plugin.scripting.host_rpc.execute_tool', return_value={'ok': True}) as mock_tool:
        handled = _maybe_dispatch_intermediate_response({'type': 'tool_call', 'id': 't1', 'tool': 'apply_document_content', 'args': {'content': ['x']}}, stdin_write=written.append)
    assert handled is True
    mock_tool.assert_called_once_with('apply_document_content', {'content': ['x']}, caller='script', allowed_tools=None, script_session_id=None, stop_checker=None, send_cancellation=None)
    assert len(written) == 1
    resp = read_pickle_frame(io.BytesIO(written[0]), require_dict=True)
    assert resp is not None
    assert resp['status'] == 'ok'
    assert resp['id'] == 't1'

def test_maybe_dispatch_tool_call_forwards_stop_and_scope():
    from plugin.scripting.venv_worker import _maybe_dispatch_intermediate_response

    def checker() -> bool:
        return False
    sentinel = object()
    written: list[bytes] = []
    with patch('plugin.scripting.host_rpc.execute_tool', return_value={'ok': True}) as mock_tool:
        handled = _maybe_dispatch_intermediate_response({'type': 'tool_call', 'id': 't1', 'tool': 'apply_document_content', 'args': {'content': ['x']}}, stdin_write=written.append, stop_checker=checker, cancellation_scope=sentinel)
    assert handled is True
    mock_tool.assert_called_once_with('apply_document_content', {'content': ['x']}, caller='script', allowed_tools=None, script_session_id=None, stop_checker=checker, send_cancellation=sentinel)
    assert len(written) == 1
    resp = read_pickle_frame(io.BytesIO(written[0]), require_dict=True)
    assert resp is not None
    assert resp['status'] == 'ok'
    assert resp['id'] == 't1'

def test_python_worker_manager_sets_is_worker_env():
    mgr = PythonWorkerManager(sys.executable, {'PATH': '/usr/bin'})
    assert mgr.env.get('WRITERAGENT_IS_WORKER') == '1'

def test_execute_ui_thread_returns_busy_when_pipe_lock_held(monkeypatch):
    """The UI thread must not block on the warm-child pipe lock."""
    monkeypatch.setattr('plugin.framework.queue_executor.main.is_current', lambda: True)
    mgr = PythonWorkerManager(sys.executable, {})
    _install_mock_proc(mgr, MagicMock(poll=MagicMock(return_value=None), stdin=io.BytesIO(), stdout=io.BytesIO()))
    assert mgr._io_lock.acquire(timeout=1)
    mgr._io_owner = threading.get_ident() + 1
    try:
        started = time.monotonic()
        result = mgr.execute('result = 1', timeout_sec=1)
        assert time.monotonic() - started < 1.0
        assert result['code'] == 'WORKER_REENTRY'
        assert 'busy' in result['message']
    finally:
        mgr._io_owner = None
        mgr._io_lock.release()

def test_execute_refuses_other_thread_during_tool_rpc():
    mgr = PythonWorkerManager(sys.executable, {})
    _install_mock_proc(mgr, MagicMock(poll=MagicMock(return_value=None), stdin=io.BytesIO(), stdout=io.BytesIO()))
    mgr._serving_tool_call = True
    mgr._io_owner = threading.get_ident() + 1
    result = mgr.execute('result = 1', timeout_sec=1)
    assert result['code'] == 'WORKER_REENTRY'
    assert 'busy' in result['message']

def test_execute_serialized_after_pipe_lock_release(monkeypatch):
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock(poll=MagicMock(return_value=None), stdin=io.BytesIO(), stdout=io.BytesIO())
    _install_mock_proc(mgr, proc)
    assert mgr._io_lock.acquire(timeout=1)
    mgr._io_owner = threading.get_ident()
    outcome: list[dict[str, Any]] = []

    def _other() -> None:
        def _fake_ipc(req: _ExecRequest, *, leased: Any) -> dict[str, Any]:
            del req, leased
            return {'status': 'ok', 'result': 1}

        with patch.object(mgr, '_execute_ipc_unlocked', side_effect=_fake_ipc):
            outcome.append(mgr.execute('result = 1', timeout_sec=1))

    waiter = threading.Thread(target=_other)
    waiter.start()
    time.sleep(0.1)
    mgr._io_owner = None
    mgr._io_lock.release()
    waiter.join(timeout=2)
    assert not waiter.is_alive()
    assert outcome and outcome[0]['status'] == 'ok'

def test_ensure_running_starts_new_session_without_preexec(monkeypatch: pytest.MonkeyPatch) -> None:
    """setsid belongs on the C spawn path, not a Python preexec_fn."""
    import plugin.framework.managed_subprocess as ms
    import plugin.scripting.venv_worker as venv_worker_module
    from plugin.scripting.venv_worker import _HARNESS_PATH
    popen = MagicMock()
    proc = MagicMock()
    proc.pid = 7
    ready_out = io.BytesIO()
    ready_out.write(pack_pickle_frame({'status': 'ready', 'pid': 7}))
    ready_out.seek(0)
    proc.stdin = io.BytesIO()
    proc.stdout = ready_out
    proc.stderr = io.BytesIO()
    proc.poll.return_value = None
    popen.return_value = proc
    drain_kwargs: list[dict[str, Any]] = []

    def fake_drain(*args: Any, **kwargs: Any) -> None:
        del args
        drain_kwargs.append(dict(kwargs))
        return None
    monkeypatch.setattr(ms.subprocess, 'Popen', popen)
    monkeypatch.setattr(ms, 'start_stderr_drain', fake_drain)
    monkeypatch.setattr(venv_worker_module, 'wrap_command_for_sandbox', lambda cmd, **_kwargs: cmd)
    mgr = PythonWorkerManager(sys.executable, {})
    monkeypatch.setattr(mgr, '_ensure_warmed_unlocked', lambda leased: None)
    try:
        mgr._ensure_running()
        call_args, kwargs = popen.call_args
        assert call_args[0] == [mgr.exe, _HARNESS_PATH]
        assert 'preexec_fn' not in kwargs
        assert 'text' not in kwargs
        assert 'encoding' not in kwargs
        assert 'errors' not in kwargs
        assert 'universal_newlines' not in kwargs
        assert kwargs['stdin'] == subprocess.PIPE
        assert kwargs['stdout'] == subprocess.PIPE
        assert kwargs['stderr'] == subprocess.PIPE
        assert kwargs['bufsize'] == 0
        assert kwargs.get('cwd') is None
        assert kwargs['env'] == mgr.env
        assert kwargs['env'] is not mgr.env
        if sys.platform == 'win32':
            assert kwargs.get('creationflags') == subprocess.CREATE_NO_WINDOW
            assert 'start_new_session' not in kwargs
        else:
            assert kwargs.get('start_new_session') is True
            assert 'creationflags' not in kwargs
        assert len(drain_kwargs) == 1
        assert drain_kwargs[0]['name'] == 'wa-venv-default-stderr'
        assert any((inst.popen is proc for inst in ms._instances))
    finally:
        mgr._terminate_worker()

def test_ensure_running_does_not_merge_parent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.framework.managed_subprocess as ms
    captured_kwargs: list[dict[str, Any]] = []
    proc = MagicMock()
    proc.pid = 9
    ready_out = io.BytesIO()
    ready_out.write(pack_pickle_frame({'status': 'ready', 'pid': 9}))
    ready_out.seek(0)
    proc.stdin = io.BytesIO()
    proc.stdout = ready_out
    proc.stderr = io.BytesIO()
    proc.poll.return_value = None
    proc.kill = MagicMock()
    proc.wait = MagicMock(return_value=0)

    def fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
        del cmd
        captured_kwargs.append(dict(kwargs))
        return proc
    monkeypatch.setattr(ms.subprocess, 'Popen', fake_popen)
    monkeypatch.setattr(ms, 'start_stderr_drain', lambda *a, **kw: None)
    mgr = PythonWorkerManager(sys.executable, {'PATH': '/only', 'WRITERAGENT_IS_WORKER': '1'})
    monkeypatch.setattr(mgr, '_ensure_warmed_unlocked', lambda leased: None)
    old_val = os.environ.get('WRITERAGENT_SPAWN_SENTINEL')
    os.environ['WRITERAGENT_SPAWN_SENTINEL'] = 'secret'
    try:
        mgr._ensure_running()
        recorded_env = captured_kwargs[0]['env']
        assert recorded_env == mgr.env
        assert 'WRITERAGENT_SPAWN_SENTINEL' not in recorded_env
        assert recorded_env is not mgr.env
    finally:
        if old_val is None:
            os.environ.pop('WRITERAGENT_SPAWN_SENTINEL', None)
        else:
            os.environ['WRITERAGENT_SPAWN_SENTINEL'] = old_val
        mgr._terminate_worker()

def test_ensure_running_popen_oserror_is_not_wrapped(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    import logging
    import plugin.framework.managed_subprocess as ms

    def fake_popen(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError('missing')
    monkeypatch.setattr(ms.subprocess, 'Popen', fake_popen)
    mgr = PythonWorkerManager(sys.executable, {})
    monkeypatch.setattr(mgr, '_ensure_warmed_unlocked', lambda leased: None)
    with caplog.at_level(logging.ERROR, logger='plugin.framework.managed_subprocess'):
        assert mgr._ensure_running() is False
    assert 'Failed to start managed subprocess' not in caplog.text
    assert _mgr_proc(mgr) is None
    assert _mgr_stderr_tail(mgr) is None

def test_script_llm_request_does_not_dispatch_host_llm(monkeypatch):
    """caller='script' (including =PY()) must not run llm_request on host credentials."""
    from plugin.scripting.venv_worker import _maybe_dispatch_intermediate_response

    def _boom(_payload):
        raise AssertionError('host LLM must not run for a non-ppt worker')
    monkeypatch.setattr('plugin.ppt_master.venv.host_rpc.handle_llm_request', _boom)
    frame = {'type': 'llm_request', 'id': '1', 'messages': [{'role': 'user', 'content': 'x'}]}
    written: list[bytes] = []
    handled = _maybe_dispatch_intermediate_response(frame, stdin_write=written.append, caller='script')
    assert handled is False
    assert written == []
    handled_default = _maybe_dispatch_intermediate_response(frame, stdin_write=written.append)
    assert handled_default is False
    assert written == []

def test_ppt_master_llm_request_still_dispatches(monkeypatch):
    from plugin.scripting.venv_worker import _maybe_dispatch_intermediate_response
    monkeypatch.setattr('plugin.ppt_master.venv.host_rpc.handle_llm_request', lambda _payload: {'status': 'ok', 'result': {'content': 'hi'}})
    written: list[bytes] = []
    handled = _maybe_dispatch_intermediate_response({'type': 'llm_request', 'id': '9', 'messages': []}, stdin_write=written.append, caller='ppt_master_venv')
    assert handled is True
    assert len(written) == 1

def test_heartbeats_cannot_extend_past_absolute_cap():
    """A heartbeat loop must stop at the ceiling, not reset the wait by grace_sec."""
    mgr = PythonWorkerManager.__new__(PythonWorkerManager)
    mgr.exe = 'python'

    def parse(_frame: bytes) -> dict[str, object]:
        return {'frame_type': FRAME_HEARTBEAT, 'payload': {'phase': 'spin'}}
    started = time.monotonic()
    with patch.object(mgr, '_read_frame_bytes', return_value=b'dummy'), patch('plugin.scripting.venv_worker.parse_worker_stdout_frame', side_effect=parse):
        with pytest.raises(subprocess.TimeoutExpired):
            mgr._read_response_with_heartbeats(io.BytesIO(), 30, 30, None, absolute_cap_sec=0.3)
    assert time.monotonic() - started < 10.0

def test_unknown_frame_does_not_extend_heartbeat_deadline(caplog: pytest.LogCaptureFixture):
    mgr = PythonWorkerManager.__new__(PythonWorkerManager)
    mgr.exe = 'python'

    def parse(_frame: bytes) -> dict[str, str]:
        return {'frame_type': 'not-a-real-frame'}
    started = time.monotonic()
    with caplog.at_level(logging.WARNING):
        with patch.object(mgr, '_read_frame_bytes', return_value=b'dummy'), patch('plugin.scripting.venv_worker.parse_worker_stdout_frame', side_effect=parse):
            with pytest.raises(subprocess.TimeoutExpired):
                mgr._read_response_with_heartbeats(io.BytesIO(), 0.25, 0.25, None, absolute_cap_sec=30)
    assert time.monotonic() - started < 10.0
    assert 'unknown frame type' in caplog.text

def test_pool_key_change_retires_old_warm_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new venv path retires the old warm child before the replacement can spawn."""
    monkeypatch.setattr(venv_worker_module, 'wrap_command_for_sandbox', lambda cmd, **_kwargs: cmd)
    shutdown_venv_workers_for_tests()
    try:
        first = PythonWorkerManager.get(sys.executable, {'PATH': os.environ.get('PATH', '/usr/bin:/bin')})
        assert first._ensure_running()
        warm = first._warm
        assert warm is not None
        with warm.lease(Deadline(30)) as leased:
            assert not isinstance(leased, dict)
            old_pid = leased.pid
        other_exe = f'{sys.executable}-wa-retire-test'
        second = PythonWorkerManager.get(other_exe, {'PATH': os.environ.get('PATH', '/usr/bin:/bin')})
        assert first._retired is True
        assert first._warm is None
        assert not pid_is_alive(old_pid)
        second.exe = sys.executable
        assert second._ensure_running()
        new_proc = _mgr_proc(second)
        assert new_proc is not None
        assert new_proc.pid != old_pid
    finally:
        shutdown_venv_workers_for_tests()

def test_warm_venv_worker_embeddings_timeout(monkeypatch):
    from plugin.scripting import venv_worker as vw
    from plugin.scripting.venv_worker import WORKER_POOL_EMBEDDINGS
    mock_execute_calls = []

    class DummyManager:

        def execute(self, action, data, timeout_sec=None, allow_heartbeat=False, **kwargs):
            mock_execute_calls.append({'action': action, 'timeout_sec': timeout_sec, 'allow_heartbeat': allow_heartbeat})
            return {'status': 'ok'}

        def warm(self):
            pass
    monkeypatch.setattr(vw, '_resolve_worker_python', lambda pool: ('dummy_exe', None))
    monkeypatch.setattr(vw.PythonWorkerManager, 'get', lambda exe, env, pool: DummyManager())
    import sys
    import types
    mod = types.ModuleType('plugin.embeddings.embedding_client')
    mod.get_embedding_model = lambda: 'dummy-model'
    sys.modules['plugin.embeddings.embedding_client'] = mod
    mod2 = types.ModuleType('plugin.scripting.config_limits')
    mod2.embeddings_worker_timeout_sec = lambda: 300
    sys.modules['plugin.scripting.config_limits'] = mod2
    try:
        vw.warm_venv_worker(pool=WORKER_POOL_EMBEDDINGS)
    finally:
        sys.modules.pop('plugin.embeddings.embedding_client', None)
        sys.modules.pop('plugin.scripting.config_limits', None)
    assert len(mock_execute_calls) == 1
    assert mock_execute_calls[0]['action'] == 'run_trusted_action'
    assert mock_execute_calls[0]['timeout_sec'] == 300
    assert mock_execute_calls[0]['allow_heartbeat'] is True

def test_read_response_with_heartbeats_swallows_callback_exceptions():
    from plugin.scripting.venv_worker import PythonWorkerManager
    from plugin.framework.ipc import FRAME_HEARTBEAT, FRAME_RESULT
    import io
    mgr = PythonWorkerManager.__new__(PythonWorkerManager)
    call_count = 0

    def mock_parse_frame(frame_bytes):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {'frame_type': FRAME_HEARTBEAT, 'payload': {'phase': 'test'}}
        return {'frame_type': FRAME_RESULT, 'status': 'ok'}
    heartbeat_calls = []

    def on_heartbeat(payload):
        heartbeat_calls.append(payload)
        raise RuntimeError('simulated ui callback error')
    with patch.object(mgr, '_read_frame_bytes', return_value=b'dummy'), patch('plugin.scripting.venv_worker.parse_worker_stdout_frame', side_effect=mock_parse_frame):
        result = mgr._read_response_with_heartbeats(stdout=io.BytesIO(), timeout_sec=10.0, grace_sec=5, on_heartbeat=on_heartbeat)
    assert result == {'frame_type': FRAME_RESULT, 'status': 'ok'}
    assert heartbeat_calls == [{'phase': 'test'}]

def test_execute_ipc_attempts_stop_checker_aborts() -> None:

    class MockProc:

        def poll(self):
            return None
    mgr = PythonWorkerManager.__new__(PythonWorkerManager)
    mgr.exe = 'python'
    _install_mock_proc(mgr, MockProc())

    class MockStdout(io.RawIOBase):

        def readable(self) -> bool:
            return True

        def readinto(self, b: bytearray) -> int:
            time.sleep(10)
            return 0
    stop_called = [False]

    def stop_checker() -> bool:
        stop_called[0] = True
        return True
    t0 = time.monotonic()
    result = mgr._read_response_bytes(io.BufferedReader(MockStdout()), timeout_sec=60, stop_checker=stop_checker)
    assert time.monotonic() - t0 < 1.0
    assert isinstance(result, FailedAfterStart)
    assert result.kind == 'cancel'
    assert stop_called[0]

def _raising_then_false_stop_checker():
    seen: list[BaseException] = []

    def stop_checker() -> bool:
        if not seen:
            err = KeyError('stop_checker blew up')
            seen.append(err)
            raise err
        return False
    return (seen, stop_checker)

def test_raising_stop_checker_on_threaded_read_returns_frame() -> None:
    """A broken stop_checker must not escape or abandon the queued frame (no-fd)."""
    payload = {'status': 'ok', 'result': 1}
    frame = pack_pickle_frame(payload)
    stdout = io.BytesIO(frame)

    class MockProc:

        def poll(self):
            return None
    mgr = PythonWorkerManager.__new__(PythonWorkerManager)
    mgr.exe = 'python'
    _install_mock_proc(mgr, MockProc())
    seen, stop_checker = _raising_then_false_stop_checker()
    result = mgr._read_response_bytes(stdout, timeout_sec=5, stop_checker=stop_checker)
    assert seen
    assert unpack_pickle_frame(result) == payload

def test_raising_stop_checker_on_select_read_returns_frame() -> None:
    """A broken stop_checker must not escape or abandon the queued frame (fd path)."""
    payload = {'status': 'ok', 'result': 1}
    frame = pack_pickle_frame(payload)
    r_fd, w_fd = os.pipe()
    os.write(w_fd, frame)
    os.close(w_fd)
    stdout = os.fdopen(r_fd, 'rb', buffering=0)

    class MockProc:

        def poll(self):
            return None
    mgr = PythonWorkerManager.__new__(PythonWorkerManager)
    mgr.exe = 'python'
    _install_mock_proc(mgr, MockProc())
    seen, stop_checker = _raising_then_false_stop_checker()
    try:
        result = mgr._read_response_bytes(stdout, timeout_sec=5, stop_checker=stop_checker)
    finally:
        stdout.close()
    assert seen
    assert unpack_pickle_frame(result) == payload

def test_read_until_terminal_uses_parsed_heartbeat_frame(monkeypatch: pytest.MonkeyPatch):
    """A heartbeat terminal frame is not unpickled a second time."""
    import plugin.scripting.venv_worker as vw
    mgr = PythonWorkerManager.__new__(PythonWorkerManager)
    mgr.exe = 'python'
    mgr._serving_tool_call = False
    parsed = {'status': 'ok', 'id': 'abc', 'result': 1, 'frame_type': 'result'}
    mgr._read_response_with_heartbeats = MagicMock(return_value=parsed)
    monkeypatch.setattr(vw, 'unpack_pickle_frame', MagicMock(side_effect=AssertionError('second unpack')))
    result = mgr._read_until_terminal(io.BytesIO(), io.BytesIO(), _ExecRequest(allow_heartbeat=True), {'id': 'abc'}, vw._TurnState(), host_read_timeout_sec=1, write_timeout_sec=1, allowed_tools=None, resolved_script_session_id=None)
    assert isinstance(result, Terminal)
    assert result.response == parsed
    mgr._read_response_with_heartbeats.assert_called_once()

def test_read_response_with_heartbeats_stop_checker() -> None:

    class MockProc:

        def poll(self):
            return None
    mgr = PythonWorkerManager.__new__(PythonWorkerManager)
    mgr.exe = 'python'
    _install_mock_proc(mgr, MockProc())
    mgr._warm = None
    stop_called = [False]

    def stop_checker() -> bool:
        stop_called[0] = True
        return True

    class MockStdout(io.BytesIO):

        def read(self, n: int=-1) -> bytes:
            return b''
    t0 = time.monotonic()
    result = mgr._read_response_with_heartbeats(MockStdout(), timeout_sec=60, grace_sec=10, on_heartbeat=None, stop_checker=stop_checker)
    assert time.monotonic() - t0 < 1.0
    assert isinstance(result, FailedAfterStart)
    assert result.kind == 'cancel'
    assert stop_called[0]

def test_host_pack_oversize_returns_error_without_terminate(monkeypatch):
    """An oversize request fails before any write; the warm worker must survive."""
    import plugin.scripting.venv_worker as venv_worker_module
    from plugin.framework.ipc import IpcFrameError

    def _pack(*args, **kwargs):
        raise IpcFrameError('payload too large')
    monkeypatch.setattr(venv_worker_module, 'pack_pickle_frame', _pack)
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    _install_mock_proc(mgr, proc)
    mgr._ensure_running = MagicMock()
    mgr._terminate_worker = MagicMock()
    result = _run_ipc_with_mock(mgr, _ExecRequest('result = 1', timeout_sec=1), proc)
    assert result['status'] == 'error'
    assert result['code'] == 'WORKER_IPC_ERROR'
    assert 'payload too large' in result['message']
    mgr._terminate_worker.assert_not_called()
    assert proc.stdin.getvalue() == b''

def test_missing_proc_after_concurrent_terminate():
    """Concurrent retire/kill during read returns an error, not AttributeError."""
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    _install_mock_proc(mgr, proc)
    mgr._write_frame_with_timeout = MagicMock()
    mgr._read_response_bytes = MagicMock(side_effect=RuntimeError('worker terminated concurrently'))
    result = _run_ipc_with_mock(mgr, _ExecRequest('result = 1', timeout_sec=1), proc)
    assert result['status'] == 'error'
    assert result['code'] == 'WORKER_IPC_ERROR'
    assert 'worker terminated concurrently' in result['message']

def test_stop_checker_returns_cancelled_code():
    """Stop during the IPC wait kills the worker, does not replay, and returns CANCELLED."""
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    _install_mock_proc(mgr, proc)
    mgr._ensure_running = MagicMock()
    mgr._terminate_worker = MagicMock()
    mgr._write_frame_with_timeout = MagicMock()
    mgr._read_response_bytes = MagicMock(return_value=FailedAfterStart(RuntimeError('Python worker stopped by user'), 'cancel'))
    result = _run_ipc_with_mock(mgr, _ExecRequest('result = 1', timeout_sec=1, stop_checker=lambda: True), proc)
    assert result['status'] == 'error'
    assert result['code'] == 'CANCELLED'
    assert result['message'] == 'Python worker stopped by user'
    assert mgr._write_frame_with_timeout.call_count == 1
    mgr._terminate_worker.assert_called_once()

def test_partial_stdin_write_delivers_full_frame():
    """A raw pipe write() can accept fewer bytes than given; the rest must still be sent."""
    mgr = PythonWorkerManager(sys.executable, {})
    stdin = MagicMock()
    written: list[bytes] = []

    def _short_write(b):
        chunk = bytes(b[:2])
        written.append(chunk)
        return len(chunk)
    stdin.write = _short_write
    mgr._write_packed_frame_with_timeout(stdin, b'12345', timeout_sec=1, label='test')
    assert written == [b'12', b'34', b'5']
    stdin.flush.assert_called_once()

class _FakeStdStream:

    def __init__(self, buf):
        self.buffer = buf

def test_harness_main_value_error_in_handle_request_returns_traceback(monkeypatch):
    import io
    from plugin.framework.ipc import read_pickle_frame
    import plugin.scripting.venv.worker_harness as harness
    req_data = pack_pickle_frame({'id': 'req-ve', 'action': 'execute', 'code': 'result = 1'})
    stdin = io.BytesIO(req_data)
    stdout = io.BytesIO()
    monkeypatch.setattr(harness.sys, 'stdin', _FakeStdStream(stdin))
    monkeypatch.setattr(harness.sys, 'stdout', _FakeStdStream(stdout))
    monkeypatch.setattr(harness.os, '_exit', lambda code: pytest.fail(f'Unexpected os._exit({code})'))
    monkeypatch.setattr(harness, 'die_with_parent', lambda: None)
    monkeypatch.setattr(harness, '_init_logging', lambda: None)

    def _raise_ve(*args, **kwargs):
        raise ValueError('custom value error')
    monkeypatch.setattr(harness, '_handle_request', _raise_ve)
    harness.main()
    _skip_harness_ready_frame(stdout)
    started = read_pickle_frame(stdout, require_dict=True)
    assert started == {'type': 'exec_started', 'id': 'req-ve'}
    res = read_pickle_frame(stdout, require_dict=True)
    assert res is not None
    assert res['id'] == 'req-ve'
    assert res['status'] == 'error'
    assert 'custom value error' in res['message']
    assert 'Invalid pickle request' not in res['message']
    assert 'traceback' in res and 'ValueError: custom value error' in res['traceback']

def test_harness_main_value_error_in_read_stage_returns_invalid_pickle_request(monkeypatch):
    import io
    import struct
    from plugin.framework.ipc import read_pickle_frame
    import plugin.scripting.venv.worker_harness as harness
    payload = pickle.dumps([1, 2, 3], protocol=5)
    bad_frame = struct.pack('!I', len(payload)) + payload
    stdin = io.BytesIO(bad_frame)
    stdout = io.BytesIO()
    monkeypatch.setattr(harness.sys, 'stdin', _FakeStdStream(stdin))
    monkeypatch.setattr(harness.sys, 'stdout', _FakeStdStream(stdout))
    monkeypatch.setattr(harness.os, '_exit', lambda code: pytest.fail(f'Unexpected os._exit({code})'))
    monkeypatch.setattr(harness, 'die_with_parent', lambda: None)
    monkeypatch.setattr(harness, '_init_logging', lambda: None)
    harness.main()
    _skip_harness_ready_frame(stdout)
    res = read_pickle_frame(stdout, require_dict=True)
    assert res is not None
    assert res['status'] == 'error'
    assert 'Invalid pickle request' in res['message']
    assert 'must contain a dict' in res['message']

def test_harness_main_ipc_frame_error_in_read_stage_breaks_without_response(monkeypatch):
    import io
    import struct
    import plugin.scripting.venv.worker_harness as harness
    bad_header = struct.pack('!I', DEFAULT_MAX_PAYLOAD_BYTES + 100) + b'extra'
    stdin = io.BytesIO(bad_header)
    stdout = io.BytesIO()
    monkeypatch.setattr(harness.sys, 'stdin', _FakeStdStream(stdin))
    monkeypatch.setattr(harness.sys, 'stdout', _FakeStdStream(stdout))
    monkeypatch.setattr(harness.os, '_exit', lambda code: pytest.fail(f'Unexpected os._exit({code})'))
    monkeypatch.setattr(harness, 'die_with_parent', lambda: None)
    monkeypatch.setattr(harness, '_init_logging', lambda: None)
    harness.main()
    _skip_harness_ready_frame(stdout)
    assert stdout.read() == b''

def test_harness_main_truncated_frame_breaks_without_response(monkeypatch, caplog):
    import io
    import plugin.scripting.venv.worker_harness as harness
    stdin = io.BytesIO(b'\x00\x00')
    stdout = io.BytesIO()
    monkeypatch.setattr(harness.sys, 'stdin', _FakeStdStream(stdin))
    monkeypatch.setattr(harness.sys, 'stdout', _FakeStdStream(stdout))
    monkeypatch.setattr(harness.os, '_exit', lambda code: pytest.fail(f'Unexpected os._exit({code})'))
    monkeypatch.setattr(harness, 'die_with_parent', lambda: None)
    monkeypatch.setattr(harness, '_init_logging', lambda: None)
    with caplog.at_level(logging.WARNING, logger='worker_harness'):
        harness.main()
    _skip_harness_ready_frame(stdout)
    assert stdout.read() == b''
    assert 'IPC frame error' in caplog.text

def test_harness_main_read_desync_during_handle_request_breaks(monkeypatch):
    import io
    from plugin.framework.ipc import IpcFrameReadError, read_pickle_frame
    import plugin.scripting.venv.worker_harness as harness
    req_data = pack_pickle_frame({'id': 'req-desync', 'action': 'execute', 'code': 'result = 1'})
    stdin = io.BytesIO(req_data)
    stdout = io.BytesIO()
    monkeypatch.setattr(harness.sys, 'stdin', _FakeStdStream(stdin))
    monkeypatch.setattr(harness.sys, 'stdout', _FakeStdStream(stdout))
    monkeypatch.setattr(harness.os, '_exit', lambda code: pytest.fail(f'Unexpected os._exit({code})'))
    monkeypatch.setattr(harness, 'die_with_parent', lambda: None)
    monkeypatch.setattr(harness, '_init_logging', lambda: None)

    def _raise_read_desync(*args, **kwargs):
        raise IpcFrameReadError('tool_call response stream desynchronized')
    monkeypatch.setattr(harness, '_handle_request', _raise_read_desync)
    harness.main()
    _skip_harness_ready_frame(stdout)
    started = read_pickle_frame(stdout, require_dict=True)
    assert started == {'type': 'exec_started', 'id': 'req-desync'}
    assert stdout.read() == b''

def test_harness_main_pack_size_error_during_handle_request_writes_error_frame(monkeypatch):
    import io
    from plugin.framework.ipc import IpcPayloadSizeError, read_pickle_frame
    import plugin.scripting.venv.worker_harness as harness
    req_data = pack_pickle_frame({'id': 'req-pack-err', 'action': 'execute', 'code': 'result = 1'})
    stdin = io.BytesIO(req_data)
    stdout = io.BytesIO()
    monkeypatch.setattr(harness.sys, 'stdin', _FakeStdStream(stdin))
    monkeypatch.setattr(harness.sys, 'stdout', _FakeStdStream(stdout))
    monkeypatch.setattr(harness.os, '_exit', lambda code: pytest.fail(f'Unexpected os._exit({code})'))
    monkeypatch.setattr(harness, 'die_with_parent', lambda: None)
    monkeypatch.setattr(harness, '_init_logging', lambda: None)

    def _raise_pack_err(*args, **kwargs):
        raise IpcPayloadSizeError('Pickle frame exceeds maximum payload size: 20000000')
    monkeypatch.setattr(harness, '_handle_request', _raise_pack_err)
    harness.main()
    _skip_harness_ready_frame(stdout)
    started = read_pickle_frame(stdout, require_dict=True)
    assert started == {'type': 'exec_started', 'id': 'req-pack-err'}
    res = read_pickle_frame(stdout, require_dict=True)
    assert res is not None
    assert res['id'] == 'req-pack-err'
    assert res['status'] == 'error'
    assert 'maximum payload size' in res['message']
    assert 'traceback' in res

def test_harness_main_user_stopped_writes_user_stopped_frame(monkeypatch):
    import io
    from plugin.framework.ipc import UserStopped, read_pickle_frame
    import plugin.scripting.venv.worker_harness as harness
    req_data = pack_pickle_frame({'id': 'req-stop', 'action': 'execute', 'code': 'result = 1'})
    stdin = io.BytesIO(req_data)
    stdout = io.BytesIO()
    monkeypatch.setattr(harness.sys, 'stdin', _FakeStdStream(stdin))
    monkeypatch.setattr(harness.sys, 'stdout', _FakeStdStream(stdout))
    monkeypatch.setattr(harness.os, '_exit', lambda code: pytest.fail(f'Unexpected os._exit({code})'))
    monkeypatch.setattr(harness, 'die_with_parent', lambda: None)
    monkeypatch.setattr(harness, '_init_logging', lambda: None)

    def _raise_user_stopped(*args, **kwargs):
        raise UserStopped('Interrupted by user.')
    monkeypatch.setattr(harness, '_handle_request', _raise_user_stopped)
    harness.main()
    _skip_harness_ready_frame(stdout)
    started = read_pickle_frame(stdout, require_dict=True)
    assert started == {'type': 'exec_started', 'id': 'req-stop'}
    res = read_pickle_frame(stdout, require_dict=True)
    assert res is not None
    assert res['id'] == 'req-stop'
    assert res['status'] == 'error'
    assert res['code'] == 'USER_STOPPED'
    assert 'Interrupted by user' in res['message']

def test_harness_main_reads_claimed_stdin(monkeypatch):
    import io
    from plugin.framework.ipc import pack_pickle_frame, read_pickle_frame
    import plugin.scripting.venv.worker_harness as harness
    req_data = pack_pickle_frame({'id': 'req-claim-in', 'action': 'execute', 'code': 'result = 1'})
    claimed_stdin = io.BytesIO(req_data)
    stdout = io.BytesIO()
    monkeypatch.setattr(harness, 'claim_ipc_stdin', lambda: claimed_stdin)
    monkeypatch.setattr(harness, 'claim_ipc_channel', lambda: stdout)
    monkeypatch.setattr(harness.os, '_exit', lambda code: pytest.fail(f'Unexpected os._exit({code})'))
    monkeypatch.setattr(harness, 'die_with_parent', lambda: None)
    monkeypatch.setattr(harness, '_init_logging', lambda: None)
    monkeypatch.setattr(harness, '_handle_request', lambda req, stdout=None: {'status': 'ok', 'result': 1})
    monkeypatch.setattr(harness.sys, 'stdin', _FakeStdStream(io.BytesIO()))
    harness.main()
    _skip_harness_ready_frame(stdout)
    started = read_pickle_frame(stdout, require_dict=True)
    assert started == {'type': 'exec_started', 'id': 'req-claim-in'}

def test_first_frame_stall_uses_timeout_not_grace(caplog: pytest.LogCaptureFixture):
    mgr = PythonWorkerManager.__new__(PythonWorkerManager)
    mgr.exe = 'python'

    def parse(_frame: bytes) -> dict[str, str]:
        return {'frame_type': 'not-a-real-frame'}
    started = time.monotonic()
    with patch.object(mgr, '_read_frame_bytes', return_value=b'dummy'), patch('plugin.scripting.venv_worker.parse_worker_stdout_frame', side_effect=parse):
        with pytest.raises(subprocess.TimeoutExpired) as excinfo:
            mgr._read_response_with_heartbeats(io.BytesIO(), timeout_sec=0.2, grace_sec=30.0, on_heartbeat=None, absolute_cap_sec=30)
    elapsed = time.monotonic() - started
    assert elapsed < 10.0
    assert excinfo.value.timeout == pytest.approx(0.2)
    unknown_logs = [rec for rec in caplog.records if rec.levelno >= logging.WARNING and 'ignoring unknown frame' in rec.getMessage()]
    assert len(unknown_logs) == 1

def test_heartbeat_then_stall_uses_grace():
    mgr = PythonWorkerManager.__new__(PythonWorkerManager)
    mgr.exe = 'python'
    call_count = 0

    def parse(_frame: bytes) -> dict[str, object]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {'frame_type': FRAME_HEARTBEAT, 'payload': {'phase': 'test'}}
        return {'frame_type': 'not-a-real-frame'}
    started = time.monotonic()
    with patch.object(mgr, '_read_frame_bytes', return_value=b'dummy'), patch('plugin.scripting.venv_worker.parse_worker_stdout_frame', side_effect=parse):
        with pytest.raises(subprocess.TimeoutExpired) as excinfo:
            mgr._read_response_with_heartbeats(io.BytesIO(), timeout_sec=0.2, grace_sec=0.4, on_heartbeat=None, absolute_cap_sec=30)
    elapsed = time.monotonic() - started
    assert elapsed >= 0.35
    assert elapsed < 10.0
    assert excinfo.value.timeout == pytest.approx(0.4)

def test_read_response_non_dict_raises_once():
    from plugin.framework.ipc import pack_pickle_frame, read_frame_payload, unpack_pickle_frame
    mgr = PythonWorkerManager.__new__(PythonWorkerManager)
    mgr.exe = 'python'
    payload = read_frame_payload(io.BytesIO(pack_pickle_frame([1])))
    assert payload is not None
    with patch.object(mgr, '_read_frame_bytes', return_value=payload), patch('plugin.scripting.venv_worker.unpack_pickle_frame', wraps=unpack_pickle_frame) as mock_unpack:
        with pytest.raises(RuntimeError, match='must be a dict'):
            mgr._read_response_with_heartbeats(io.BytesIO(), timeout_sec=1.0, grace_sec=1.0, on_heartbeat=None)
        assert mock_unpack.call_count == 1

def test_read_response_empty_dict_is_terminal():
    from plugin.framework.ipc import pack_pickle_frame, read_frame_payload, unpack_pickle_frame
    mgr = PythonWorkerManager.__new__(PythonWorkerManager)
    mgr.exe = 'python'
    payload = read_frame_payload(io.BytesIO(pack_pickle_frame({})))
    assert payload is not None
    with patch.object(mgr, '_read_frame_bytes', return_value=payload), patch('plugin.scripting.venv_worker.unpack_pickle_frame', wraps=unpack_pickle_frame) as mock_unpack:
        res = mgr._read_response_with_heartbeats(io.BytesIO(), timeout_sec=1.0, grace_sec=1.0, on_heartbeat=None)
        assert res == {}
        assert mock_unpack.call_count == 1

def test_allowed_tools_resolved_before_request_write(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    _install_mock_proc(mgr, proc)
    mgr._ensure_running = MagicMock()
    events: list[str] = []

    def _resolve(domain: str | None) -> frozenset[str]:
        events.append('resolve')
        return frozenset()
    monkeypatch.setattr('plugin.scripting.host_rpc.resolve_allowed_tools', _resolve)

    def _write(stdin: Any, message: Any, **kwargs: Any) -> None:
        events.append('write')
    mgr._write_frame_with_timeout = _write

    def _read(stdout: Any, stdin: Any, req: Any, wire: dict[str, Any], *args: Any, **kwargs: Any) -> Terminal:
        return Terminal({'id': wire['id'], 'status': 'ok', 'result': 1})
    mgr._read_until_terminal = _read
    result = _run_ipc_with_mock(mgr, _ExecRequest('result = 1', timeout_sec=1, python_tool_domain=''), proc)
    assert result['status'] == 'ok'
    assert result['result'] == 1
    assert events == ['resolve', 'write']

def test_resolve_allowed_tools_failure_does_not_write(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    _install_mock_proc(mgr, proc)
    mgr._ensure_running = MagicMock()

    def _fail_resolve(domain: str | None) -> frozenset[str]:
        raise RuntimeError('domain map failed')
    monkeypatch.setattr('plugin.scripting.host_rpc.resolve_allowed_tools', _fail_resolve)
    mgr._write_frame_with_timeout = MagicMock()
    mgr._terminate_worker = MagicMock()
    result = _run_ipc_with_mock(mgr, _ExecRequest('result = 1', timeout_sec=1), proc)
    assert result['status'] == 'error'
    assert result['code'] == 'WORKER_IPC_ERROR'
    assert 'domain map failed' in result['message']
    assert mgr._write_frame_with_timeout.call_count == 0
    assert mgr._terminate_worker.call_count == 0

def test_invalid_pickle_request_frame_keeps_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    _install_mock_proc(mgr, proc)
    mgr._ensure_running = MagicMock()
    mgr._write_frame_with_timeout = MagicMock()
    mgr._terminate_worker = MagicMock()
    mgr._read_response_bytes = MagicMock(return_value=pickle.dumps({'id': '', 'status': 'error', 'message': 'Invalid pickle request: nope'}, protocol=5))
    cleared: list[bool] = []
    monkeypatch.setattr('plugin.calc.python.function.clear_python_addin_cache', lambda: cleared.append(True))
    result = _run_ipc_with_mock(mgr, _ExecRequest('result = 1', timeout_sec=1), proc)
    assert result['code'] == 'WORKER_IPC_ERROR'
    assert 'Invalid pickle request: nope' in result['message']
    assert 'id mismatch' not in result['message']
    assert mgr._write_frame_with_timeout.call_count == 1
    assert mgr._terminate_worker.call_count == 0
    assert cleared == []

def test_empty_id_error_after_exec_started_still_kills() -> None:
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    _install_mock_proc(mgr, proc)
    mgr._ensure_running = MagicMock()
    captured: dict[str, dict[str, Any]] = {}
    writes = {'n': 0}

    def _write_frame(stdin: Any, request: dict[str, Any], **kwargs: Any) -> None:
        writes['n'] += 1
        captured['request'] = request
    mgr._write_frame_with_timeout = _write_frame
    reads = {'n': 0}

    def _read(stdout: Any, timeout_sec: float | int, stop_checker: Any=None) -> bytes:
        reads['n'] += 1
        request = captured['request']
        if reads['n'] == 1:
            return pickle.dumps({'type': 'exec_started', 'id': request['id']}, protocol=5)
        return pickle.dumps({'id': '', 'status': 'error', 'message': 'Invalid pickle request: late'}, protocol=5)
    mgr._read_response_bytes = _read
    mgr._terminate_worker = MagicMock()
    result = _run_ipc_with_mock(mgr, _ExecRequest('result = 1', timeout_sec=1), proc)
    assert result['code'] == 'WORKER_IPC_ERROR'
    assert 'id mismatch' in result['message']
    assert writes['n'] == 1
    assert mgr._terminate_worker.call_count == 1
    assert mgr._terminate_worker.call_args_list == [call()]

def test_ensure_running_popen_runs_on_supervisor_spawner_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.framework.managed_subprocess as ms
    recorded_ident: int | None = None

    def fake_popen(*args: Any, **kwargs: Any) -> Any:
        nonlocal recorded_ident
        recorded_ident = threading.get_ident()
        proc = MagicMock()
        proc.pid = os.getpid()
        proc.stdin = io.BytesIO()
        proc.stdout = io.BytesIO()
        proc.stdout.write(pack_pickle_frame({'status': 'ready', 'pid': proc.pid}))
        proc.stdout.seek(0)
        proc.stderr = io.BytesIO()
        proc.poll.return_value = None
        proc.wait.return_value = 0
        proc.kill.return_value = None
        return proc
    monkeypatch.setattr(ms.subprocess, 'Popen', fake_popen)
    monkeypatch.setattr('plugin.framework.managed_subprocess.start_stderr_drain', lambda *args, **kwargs: None)
    monkeypatch.setattr('plugin.scripting.venv_worker.wrap_command_for_sandbox', lambda cmd, *args, **kwargs: cmd)
    mgr = PythonWorkerManager(sys.executable, {'PATH': os.environ.get('PATH', '')})
    helper_ident: int | None = None

    def helper_run() -> None:
        nonlocal helper_ident
        helper_ident = threading.get_ident()
        mgr._ensure_running()
    try:
        assert supervisor.spawner_ident is not None
        t = threading.Thread(target=helper_run)
        t.start()
        t.join()
        assert recorded_ident == supervisor.spawner_ident
        assert recorded_ident != helper_ident
    finally:
        mgr._retired = True
        mgr._terminate_worker()
        shutdown_venv_workers_for_tests()

def test_caller_thread_exit_does_not_pdeathsig_the_worker() -> None:
    from plugin.scripting.sandbox import detect_sandbox
    if sys.platform != 'linux' or detect_sandbox() == 'flatpak':
        pytest.skip('Linux host-only parent-death signal test')
    mgr = PythonWorkerManager(sys.executable, {'PATH': os.environ.get('PATH', '/usr/bin')})
    helper_err: BaseException | None = None

    def run_start() -> None:
        nonlocal helper_err
        try:
            mgr._ensure_running()
        except BaseException as exc:
            helper_err = exc
    try:
        t = threading.Thread(target=run_start)
        t.start()
        t.join()
        if helper_err is not None:
            stderr = mgr._drain_stderr()
            raise AssertionError(f'_ensure_running failed on helper: {helper_err}{stderr}') from helper_err

        # A round trip proves the harness is past die_with_parent (main() arms
        # PR_SET_PDEATHSIG before its first stdin read). A fixed sleep raced
        # interpreter startup under CI load: if the spawn thread exited before
        # prctl ran, nothing was armed and the worker never died.
        result = mgr.execute('result = 42', timeout_sec=30)
        assert result.get('status') == 'ok', result
        assert _mgr_proc(mgr) is not None
        assert _mgr_proc(mgr).poll() is None

        # The kernel delivers SIGKILL when the spawn OS thread exits, which can
        # trail Thread.join() slightly; allow for a loaded CI runner.
        deadline = time.monotonic() + 15.0
        exit_code = None
        while time.monotonic() < deadline:
            exit_code = _mgr_proc(mgr).poll()
            if exit_code is not None:
                break
            time.sleep(0.05)
        assert exit_code is None, 'worker died when a short-lived caller thread exited'
    finally:
        if _mgr_proc(mgr) is not None and _mgr_proc(mgr).poll() is None:
            mgr._terminate_worker()
        shutdown_venv_workers_for_tests()

class _ProcLoads:

    def __init__(self, values: list[object]) -> None:
        self.values = values
        self.seen: list[object] = []

    def __set__(self, obj: object, value: object) -> None:
        del obj, value

    def __get__(self, obj: object, objtype: type | None=None) -> Any:
        if obj is None:
            return self
        val = self.values[min(len(self.seen), len(self.values) - 1)]
        self.seen.append(val)
        return val

    def __call__(self) -> Any:
        val = self.values[min(len(self.seen), len(self.values) - 1)]
        self.seen.append(val)
        return val

class _MockProc:

    def __init__(self, returncode: int | None=None) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode

def test_is_worker_alive_reads_proc_once(monkeypatch: pytest.MonkeyPatch) -> None:
    alive = _MockProc(None)
    loads = _ProcLoads([alive, None])
    mgr = PythonWorkerManager(sys.executable, {})
    monkeypatch.setattr(mgr, '_worker_popen', loads)
    assert mgr._is_worker_alive() is True
    assert loads.seen == [alive]
    loads.seen.clear()
    loads.values = [None]
    assert mgr._is_worker_alive() is False
    assert loads.seen == [None]
    loads.seen.clear()
    dead = _MockProc(0)
    loads.values = [dead, None]
    assert mgr._is_worker_alive() is False
    assert loads.seen == [dead]

def test_execute_stop_checker_returns_cancelled_before_io(monkeypatch):
    monkeypatch.setattr('plugin.framework.queue_executor.main.is_current', lambda: False)
    mgr = PythonWorkerManager('/tmp/python-test', {'PATH': '/usr/bin'})
    _install_mock_proc(mgr, MagicMock(poll=MagicMock(return_value=None), stdin=io.BytesIO(), stdout=io.BytesIO()))
    res = mgr.execute('result = 1', timeout_sec=1, stop_checker=lambda: True)
    assert res['code'] == 'CANCELLED'

def test_ensure_running_does_not_respawn_live_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(venv_worker_module, 'wrap_command_for_sandbox', lambda cmd, **_kwargs: cmd)
    mgr = PythonWorkerManager(sys.executable, {'PATH': os.environ.get('PATH', '')})
    assert mgr._ensure_running()
    pid1 = _mgr_proc(mgr).pid
    assert pid1 is not None
    warm = mgr._warm
    assert warm is not None
    with patch.object(warm._supervisor, 'spawn') as mock_spawn:
        assert mgr._ensure_running()
        mock_spawn.assert_not_called()
    assert _mgr_proc(mgr).pid == pid1
    mgr._terminate_worker()

def test_parse_worker_stdout_frame_rejects_non_dict():
    payload = pickle.dumps([1], protocol=5)
    with pytest.raises(RuntimeError, match='Worker response must be a dict'):
        parse_worker_stdout_frame(payload)

def test_parse_worker_stdout_frame_returns_empty_dict():
    payload = pickle.dumps({}, protocol=5)
    assert parse_worker_stdout_frame(payload) == {}

def test_parse_worker_stdout_frame_empty_bytes_raise():
    with pytest.raises(ValueError):
        parse_worker_stdout_frame(b'')

def test_run_in_session_does_not_warm_when_stopped():
    mgr = PythonWorkerManager('/tmp/python-test', {'PATH': '/usr/bin'})
    mock_warm = MagicMock()
    mgr._ensure_warmed_unlocked = mock_warm
    res = mgr._run_in_session(lambda: {'status': 'ok'}, stop_checker=lambda: True)
    assert res['status'] == 'error'
    assert res['code'] == 'CANCELLED'
    mock_warm.assert_not_called()

def test_host_preparation_failure_does_not_kill_worker(monkeypatch):
    mgr = PythonWorkerManager(sys.executable, {'PATH': os.environ.get('PATH', '')})
    monkeypatch.setattr('plugin.scripting.host_rpc.resolve_allowed_tools', MagicMock(side_effect=RuntimeError('boom')))
    terminate_calls: list[Any] = []
    monkeypatch.setattr(mgr, '_terminate_worker', lambda *args, **kwargs: terminate_calls.append((args, kwargs)))
    mgr._ensure_warmed_unlocked = lambda *args, **kwargs: None
    res = mgr.execute('result = 1', timeout_sec=5)
    assert res['status'] == 'error'
    assert res['code'] == 'WORKER_IPC_ERROR'
    assert 'boom' in res['message']
    assert len(terminate_calls) == 0

def test_non_default_pool_failure_does_not_clear_calc_addin_cache(monkeypatch):
    from plugin.framework.constants import WORKER_POOL_DEFAULT, WORKER_POOL_DIAGNOSTICS
    clear_calls: list[int] = []
    monkeypatch.setattr('plugin.calc.python.function.clear_python_addin_cache', lambda: clear_calls.append(1))
    diag_mgr = PythonWorkerManager('/tmp/python-diag', {'PATH': '/usr/bin'}, pool=WORKER_POOL_DIAGNOSTICS)
    monkeypatch.setattr(diag_mgr, '_terminate_worker', lambda *args, **kwargs: None)
    diag_mgr._fail_no_replay('WORKER_IPC_ERROR', 'x')
    assert len(clear_calls) == 0
    default_mgr = PythonWorkerManager('/tmp/python-default', {'PATH': '/usr/bin'}, pool=WORKER_POOL_DEFAULT)
    monkeypatch.setattr(default_mgr, '_terminate_worker', lambda *args, **kwargs: None)
    default_mgr._fail_no_replay('WORKER_IPC_ERROR', 'x')
    assert len(clear_calls) == 1

def test_maybe_dispatch_ppt_master_missing_dependency_reraises(monkeypatch):
    import builtins
    from plugin.scripting.venv_worker import _maybe_dispatch_intermediate_response
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'plugin.ppt_master.venv.host_rpc' or (name == 'plugin.ppt_master' and fromlist and ('venv' in fromlist)):
            raise ModuleNotFoundError("No module named 'numpy'", name='numpy')
        return real_import(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, '__import__', fake_import)
    with pytest.raises(ModuleNotFoundError) as exc_info:
        _maybe_dispatch_intermediate_response({'status': 'ok', 'result': 2}, stdin_write=MagicMock(), caller='ppt_master_venv')
    assert exc_info.value.name == 'numpy'

@pytest.mark.skipif(sys.platform != 'linux', reason='SIGKILL idle-death check is Linux-only')
def test_idle_worker_death_clears_matrix_sessions_and_runs_on_death_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.calc import python as calc_python
    clear_calls: list[int] = []
    real_clear = calc_python.function.clear_python_addin_cache

    def clear_spy() -> None:
        clear_calls.append(1)
        real_clear()
    monkeypatch.setattr('plugin.calc.python.function.clear_python_addin_cache', clear_spy)
    monkeypatch.setattr(venv_worker_module, 'wrap_command_for_sandbox', lambda cmd, **_kwargs: cmd)
    mgr = PythonWorkerManager(sys.executable, {'PATH': os.environ.get('PATH', '')})
    try:
        warm = mgr._get_warm()
        assert warm is not None
        with warm.lease(Deadline(30)) as leased:
            assert not isinstance(leased, dict)
            pid = leased.pid
        stale_key = (1, ('stale',), 'idle-death')
        with calc_python.function._MATRIX_SCALAR_SESSIONS_LOCK:
            calc_python.function._MATRIX_SCALAR_SESSIONS[stale_key] = calc_python.function.WorkerResultSession([99], [99])
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.2)
        res = mgr.execute('result = 1', timeout_sec=10)
        assert res.get('status') == 'ok'
        assert len(clear_calls) == 1
        with calc_python.function._MATRIX_SCALAR_SESSIONS_LOCK:
            assert stale_key not in calc_python.function._MATRIX_SCALAR_SESSIONS
    finally:
        mgr._retired = True
        shutdown_venv_workers_for_tests()

@pytest.mark.slow
@pytest.mark.skipif(sys.platform != 'linux', reason='orphan PDEATHSIG check is Linux-only')
def test_orphan_venv_harness_dies_with_parent(tmp_path: Any) -> None:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    harness = os.path.join(root, 'plugin', 'scripting', 'venv', 'worker_harness.py')
    parent_script = tmp_path / 'parent_venv_warm.py'
    parent_script.write_text('\n'.join(['import os, sys, time', f'sys.path.insert(0, {root!r})', 'from plugin.framework.process_worker import Deadline', 'from plugin.framework.warm_child import ChildSpec, WarmChild', 'from plugin.scripting.venv_worker import wrap_command_for_sandbox', f'harness = {harness!r}', 'env = {k: v for k, v in os.environ.items() if isinstance(v, str)}', "env['WRITERAGENT_IS_WORKER'] = '1'", 'spec = ChildSpec(', "    name='orphan-venv',", '    argv=[sys.executable, harness],', '    env=env,', '    command_wrapper=wrap_command_for_sandbox,', '    ready_timeout_sec=30.0,', ')', 'warm = WarmChild(spec)', 'with warm.lease(Deadline(60)) as leased:', '    if isinstance(leased, dict):', '        raise SystemExit(2)', '    pid = leased.pid', "    with open(sys.argv[1], 'w', encoding='utf-8') as f:", '        f.write(str(pid))', 'time.sleep(120)']), encoding='utf-8')
    pid_file = tmp_path / 'child.pid'
    parent = subprocess.Popen([sys.executable, str(parent_script), str(pid_file)])
    try:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if pid_file.is_file():
                break
            time.sleep(0.05)
        assert pid_file.is_file(), 'parent never recorded child pid'
        child_pid = int(pid_file.read_text(encoding='utf-8').strip())
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5)
        gone_by = time.monotonic() + 2.0
        while time.monotonic() < gone_by:
            try:
                os.kill(child_pid, 0)
            except OSError:
                return
            time.sleep(0.05)
        pytest.fail(f'venv harness child {child_pid} outlived SIGKILL parent')
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)

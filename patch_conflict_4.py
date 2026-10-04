import re

with open("tests/scripting/test_venv_worker.py", "r") as f:
    content = f.read()

conflict_block = """<<<<<<< HEAD
def test_read_response_with_heartbeats_swallows_callback_exceptions():
    from plugin.scripting.venv_worker import PythonWorkerManager
    from plugin.scripting.venv.worker_heartbeat import FRAME_HEARTBEAT, FRAME_RESULT
    import io

    mgr = PythonWorkerManager.__new__(PythonWorkerManager)

    # Mock parse_frame to first return a heartbeat, then a result frame
    call_count = 0
    def mock_parse_frame(frame_bytes):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"frame_type": FRAME_HEARTBEAT, "payload": {"phase": "test"}}
        return {"frame_type": FRAME_RESULT, "status": "ok"}

    import plugin.scripting.venv_worker as vw

    # Track calls to on_heartbeat
    heartbeat_calls = []
    def on_heartbeat(payload):
        heartbeat_calls.append(payload)
        raise RuntimeError("simulated ui callback error")

    with patch.object(mgr, "_read_frame_bytes", return_value=b"dummy"), \\
         patch("plugin.scripting.venv.worker_heartbeat.parse_frame", side_effect=mock_parse_frame):

        # It shouldn't crash, it should return the b"dummy" frame ultimately
        result = mgr._read_response_with_heartbeats(
            stdout=io.BytesIO(),
            timeout_sec=10.0,
            grace_sec=5,
            on_heartbeat=on_heartbeat
        )

    assert result == b"dummy"
    assert heartbeat_calls == [{"phase": "test"}]
=======
def test_venv_worker_honor_stop():
    from unittest.mock import Mock
    import io
    from plugin.scripting.venv_worker import PythonWorkerManager
    manager = PythonWorkerManager(exe="python", env={})

    stop_checker = Mock(return_value=True)
    stdout = io.BytesIO(b"fake data")

    with pytest.raises(subprocess.TimeoutExpired):
        manager._read_response_with_heartbeats(
            stdout=stdout,
            timeout_sec=10,
            grace_sec=10,
            on_heartbeat=None,
            stop_checker=stop_checker,
        )
>>>>>>> 7a1c0d1c (Fix ppt-master template fill text injection, venv stop handling, and export concurrency)"""

resolved_block = """def test_read_response_with_heartbeats_swallows_callback_exceptions():
    from plugin.scripting.venv_worker import PythonWorkerManager
    from plugin.scripting.venv.worker_heartbeat import FRAME_HEARTBEAT, FRAME_RESULT
    import io

    mgr = PythonWorkerManager.__new__(PythonWorkerManager)

    # Mock parse_frame to first return a heartbeat, then a result frame
    call_count = 0
    def mock_parse_frame(frame_bytes):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"frame_type": FRAME_HEARTBEAT, "payload": {"phase": "test"}}
        return {"frame_type": FRAME_RESULT, "status": "ok"}

    import plugin.scripting.venv_worker as vw

    # Track calls to on_heartbeat
    heartbeat_calls = []
    def on_heartbeat(payload):
        heartbeat_calls.append(payload)
        raise RuntimeError("simulated ui callback error")

    with patch.object(mgr, "_read_frame_bytes", return_value=b"dummy"), \\
         patch("plugin.scripting.venv.worker_heartbeat.parse_frame", side_effect=mock_parse_frame):

        # It shouldn't crash, it should return the b"dummy" frame ultimately
        result = mgr._read_response_with_heartbeats(
            stdout=io.BytesIO(),
            timeout_sec=10.0,
            grace_sec=5,
            on_heartbeat=on_heartbeat
        )

    assert result == b"dummy"
    assert heartbeat_calls == [{"phase": "test"}]

def test_venv_worker_honor_stop():
    from unittest.mock import Mock
    import io
    from plugin.scripting.venv_worker import PythonWorkerManager
    manager = PythonWorkerManager(exe="python", env={})

    stop_checker = Mock(return_value=True)
    stdout = io.BytesIO(b"fake data")

    with pytest.raises(subprocess.TimeoutExpired):
        manager._read_response_with_heartbeats(
            stdout=stdout,
            timeout_sec=10,
            grace_sec=10,
            on_heartbeat=None,
            stop_checker=stop_checker,
        )"""

content = content.replace(conflict_block, resolved_block)

with open("tests/scripting/test_venv_worker.py", "w") as f:
    f.write(content)

import pytest
from plugin.framework.worker_pool import start_stderr_drain
import subprocess
import sys
import time

def test_stderr_drain_prevents_pipe_deadlock():
    """Child floods stderr before reading stdin; parent must not hang on the write/read."""
    script = (
        "import sys\n"
        "sys.stderr.write('x' * (128 * 1024))\n"
        "sys.stderr.flush()\n"
        "line = sys.stdin.buffer.readline()\n"
        "sys.stdout.buffer.write(b'ok:' + line)\n"
        "sys.stdout.buffer.flush()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    drain = start_stderr_drain(proc.stderr, max_tail_chars=256 * 1024, name="test-stderr-flood")
    assert drain is not None
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(b"hello\n")
    proc.stdin.flush()
    deadline = time.time() + 10.0
    out = b""
    while time.time() < deadline and b"\n" not in out:
        chunk = proc.stdout.read(64)
        if not chunk:
            break
        out += chunk
    proc.wait(timeout=5)
    assert out.startswith(b"ok:hello")
    # Add a small wait for the drain to finish processing
    time.sleep(0.5)
    assert len(drain.text()) >= 128 * 1024

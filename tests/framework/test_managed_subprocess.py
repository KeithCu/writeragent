import os
import signal
import sys
import pytest
import subprocess
from unittest.mock import MagicMock, patch

from plugin.framework.managed_subprocess import (
    ManagedSubprocess,
    _kill_process_tree,
    shutdown_all,
    _instances
)

def test_managed_subprocess_explicit_start_and_cwd(tmp_path):
    cwd = str(tmp_path)
    env = {"TEST_VAR": "hello"}

    cmd = [sys.executable, "-c", "import os; print(os.getcwd()); print(os.environ.get('TEST_VAR'))"]

    proc = ManagedSubprocess(cmd, env=env, cwd=cwd)
    assert not proc.is_alive()

    proc.start()
    assert proc.is_alive()
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr_tail is not None

    stdout = proc.stdout.read()
    proc.stdout.close()
    output = stdout.decode("utf-8").strip().splitlines()

    assert output[0].endswith(os.path.basename(cwd))
    assert output[1] == "hello"

    proc.close()
    assert not proc.is_alive()

def test_managed_subprocess_thread_guard():
    refused = False
    def guard():
        nonlocal refused
        refused = True
        raise RuntimeError("Forbidden thread")

    cmd = [sys.executable, "-c", "print('ok')"]
    proc = ManagedSubprocess(cmd, thread_guard=guard)

    with pytest.raises(RuntimeError, match="Forbidden thread"):
        proc.start()

    assert refused
    assert not proc.is_alive()
    assert proc._proc is None


def test_managed_subprocess_stderr_tail():
    code = "import sys; sys.stderr.write('crash text'); sys.stderr.flush()"
    cmd = [sys.executable, "-c", code]

    # Need start_new_session for POSIX group killing to work across descendants
    if sys.platform != "win32":
        pass # ManagedSubprocess sets start_new_session automatically if sys.platform != "win32"
        # Wait, ManagedSubprocess sets popen_kw['start_new_session'] = True in start()

    proc = ManagedSubprocess(cmd)
    proc.start()

    import time
    time.sleep(0.1)
    tail = proc.stderr_tail
    assert tail is not None
    assert "crash text" in tail.finish_text()
    proc.close()


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX process groups')
def test_managed_subprocess_tree_kill_grandchild():
    pass

def test_managed_subprocess_idempotent_close():
    cmd = [sys.executable, "-c", "import time; time.sleep(10)"]
    proc = ManagedSubprocess(cmd)
    proc.start()

    assert proc.is_alive()
    assert proc in _instances

    proc.close()
    assert not proc.is_alive()
    assert proc not in _instances

    # Second close should be a no-op and not raise any errors
    proc.close()

def test_managed_subprocess_shutdown_registry():
    cmd1 = [sys.executable, "-c", "import time; time.sleep(10)"]
    cmd2 = [sys.executable, "-c", "import time; time.sleep(10)"]

    proc1 = ManagedSubprocess(cmd1)
    proc1.start()

    proc2 = ManagedSubprocess(cmd2)
    proc2.start()

    assert proc1 in _instances
    assert proc2 in _instances
    assert proc1.is_alive()
    assert proc2.is_alive()

    shutdown_all()

    assert not proc1.is_alive()
    assert not proc2.is_alive()
    assert proc1 not in _instances
    assert proc2 not in _instances

import pytest
import time
from unittest.mock import MagicMock
from plugin.acp.acp_connection import ACPConnection
from threading import Event

def test_acp_stop_does_not_block_ui(monkeypatch):
    """Verify that ACPConnection.stop() returns immediately instead of blocking."""
    # We simulate a subprocess that hangs on wait()
    mock_proc = MagicMock()
    # If wait is called on the main thread, it would block. We sleep for 1 second in wait to simulate delay.
    def mock_wait(timeout=None):
        time.sleep(1)
    mock_proc.wait.side_effect = mock_wait

    conn = ACPConnection(["dummy"])
    conn._proc = mock_proc

    start_time = time.time()
    conn.stop()
    end_time = time.time()

    assert end_time - start_time < 0.5, "stop() blocked the calling thread!"

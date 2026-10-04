import time
from unittest.mock import MagicMock
from plugin.acp.acp_connection import ACPConnection

def test_acp_notification_does_not_block():
    conn = ACPConnection(["dummy"])
    mock_proc = MagicMock()

    def mock_write(b):
        time.sleep(1)

    mock_stdin = MagicMock()
    mock_stdin.write.side_effect = mock_write
    mock_proc.stdin = mock_stdin

    conn._proc = mock_proc

    start = time.time()
    conn.send_notification("session/cancel", {})
    end = time.time()

    assert end - start < 0.5, "send_notification blocked!"

def test_acp_response_does_not_block():
    conn = ACPConnection(["dummy"])
    mock_proc = MagicMock()

    def mock_write(b):
        time.sleep(1)

    mock_stdin = MagicMock()
    mock_stdin.write.side_effect = mock_write
    mock_proc.stdin = mock_stdin

    conn._proc = mock_proc

    start = time.time()
    conn.send_response("id_123", result={})
    end = time.time()

    assert end - start < 0.5, "send_response blocked!"

def test_acp_stop_does_not_block():
    conn = ACPConnection(["dummy"])
    mock_proc = MagicMock()

    def mock_close():
        time.sleep(1)

    mock_stdin = MagicMock()
    mock_stdin.close.side_effect = mock_close
    mock_proc.stdin = mock_stdin

    conn._proc = mock_proc

    start = time.time()
    conn.stop()
    end = time.time()

    assert end - start < 0.5, "stop blocked on stdin.close!"

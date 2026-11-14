import threading
from unittest.mock import MagicMock, patch

import pytest

from plugin.acp.acp_connection import ACPConnection


@pytest.mark.parametrize("kind", ["notification", "response"])
def test_acp_write_does_not_block(kind: str) -> None:
    conn = ACPConnection(["dummy"])
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    write_started = threading.Event()
    release_write = threading.Event()

    def blocking_write(_data: bytes) -> None:
        write_started.set()
        release_write.wait(timeout=5)

    mock_stdin = MagicMock()
    mock_stdin.write.side_effect = blocking_write
    mock_proc.stdin = mock_stdin
    conn._proc = mock_proc

    with patch("plugin.acp.acp_connection._kill_acp_tree"):
        if kind == "notification":
            conn.send_notification("session/cancel", {})
        else:
            conn.send_response("id_123", result={})

        # Caller returned while stdin.write is still blocked on release_write.
        assert write_started.wait(timeout=5)
        mock_stdin.write.assert_called_once()
        release_write.set()

        writer = conn._writer_thread
        conn.stop()
        if writer is not None:
            writer.join(timeout=5)


@pytest.mark.parametrize("blocking_point", ["stdin.close", "proc.wait"])
def test_acp_stop_does_not_block(blocking_point: str) -> None:
    conn = ACPConnection(["dummy"])
    mock_proc = MagicMock()

    entered = threading.Event()
    release = threading.Event()

    def block(*_args, **_kwargs) -> None:
        entered.set()
        release.wait(timeout=5)

    mock_stdin = MagicMock()
    if blocking_point == "stdin.close":
        mock_stdin.close.side_effect = block
    else:
        mock_proc.wait.side_effect = block
    mock_proc.stdin = mock_stdin
    conn._proc = mock_proc

    with patch("plugin.acp.acp_connection._kill_acp_tree"):
        conn.stop()
        # stop() returned while the background worker is blocked in close/wait.
        assert entered.wait(timeout=5)
        release.set()

        if conn._writer_thread is not None:
            conn._writer_thread.join(timeout=5)

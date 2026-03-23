import pytest
import time
from unittest.mock import MagicMock, patch

from plugin.framework.errors import NetworkError, format_error_payload
from plugin.framework.retry_decorator import retry_with_backoff
from plugin.framework.errors import handle_network_error
from plugin.modules.http.mcp_protocol import BusyError

def test_retry_success_on_first_try():
    mock_func = MagicMock(return_value="success")

    @retry_with_backoff(max_attempts=3, base_delay=0.01)
    def test_func():
        return mock_func()

    result = test_func()
    assert result == "success"
    assert mock_func.call_count == 1

def test_retry_success_after_failure():
    mock_func = MagicMock(side_effect=[ConnectionError("fail"), "success"])

    @retry_with_backoff(max_attempts=3, base_delay=0.01)
    def test_func():
        return mock_func()

    result = test_func()
    assert result == "success"
    assert mock_func.call_count == 2

def test_retry_exhaustion_raises_network_error():
    mock_func = MagicMock(side_effect=ConnectionError("fail"))

    @retry_with_backoff(max_attempts=3, base_delay=0.01)
    def test_func():
        return mock_func()

    with pytest.raises(NetworkError) as excinfo:
        test_func()

    assert "Operation failed after 3 attempts" in str(excinfo.value)
    assert excinfo.value.code == "NETWORK_RETRY_FAILED"
    assert mock_func.call_count == 3

def test_retry_ignores_unspecified_exceptions():
    mock_func = MagicMock(side_effect=ValueError("fail"))

    @retry_with_backoff(max_attempts=3, base_delay=0.01, retry_exceptions=(ConnectionError,))
    def test_func():
        return mock_func()

    with pytest.raises(ValueError) as excinfo:
        test_func()

    assert mock_func.call_count == 1

@patch("time.sleep")
def test_retry_backoff_timing(mock_sleep):
    mock_func = MagicMock(side_effect=ConnectionError("fail"))

    @retry_with_backoff(max_attempts=3, base_delay=0.1, max_delay=1.0)
    def test_func():
        return mock_func()

    with pytest.raises(NetworkError):
        test_func()

    assert mock_sleep.call_count == 2
    # First sleep base_delay * (2^0) = 0.1 * jitter
    # Second sleep base_delay * (2^1) = 0.2 * jitter
    # Just check sleep was called

def test_handle_network_error_with_network_error():
    err = NetworkError("Test error", code="TEST_CODE")
    payload = handle_network_error(err, "test_context")

    assert payload["status"] == "error"
    assert payload["code"] == "TEST_CODE"
    assert payload["message"] == "Test error"

def test_handle_network_error_with_other_exception():
    err = ValueError("Test error")
    payload = handle_network_error(err, "test_context")

    assert payload["status"] == "error"
    assert payload["code"] == "NETWORK_WRAPPED_ERROR"
    assert "Network-related error in test_context" in payload["message"]
    assert payload["details"]["type"] == "ValueError"

def test_mcp_busy_error_retry():
    mock_func = MagicMock(side_effect=[BusyError("busy"), "success"])

    @retry_with_backoff(max_attempts=3, base_delay=0.01, retry_exceptions=(BusyError,))
    def execute():
        return mock_func()

    assert execute() == "success"
    assert mock_func.call_count == 2

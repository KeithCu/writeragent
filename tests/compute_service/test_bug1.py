from compute_service.json_forward import parse_execute_request, ExecuteRequestError
import pytest

def test_nested_nonfinite_id_rejected():
    with pytest.raises(ExecuteRequestError):
        payload = b'{"id": [1e9999], "code": "1"}'
        parse_execute_request(payload, None)

def test_non_scalar_timeout_ms_rejected():
    with pytest.raises(ExecuteRequestError):
        payload = b'{"timeout_ms": [1000], "code": "1"}'
        parse_execute_request(payload, None)

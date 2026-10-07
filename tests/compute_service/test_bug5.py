from compute_service.json_peel import peel_execute_request, ExecuteRequestError
import pytest

def test_skip_string_invalid_unicode_escape_malformed():
    with pytest.raises(ExecuteRequestError, match="Invalid unicode escape|Unterminated escape"):
        payload = b'{"id": "\\u12",", "code": "1"}'
        peel_execute_request(payload)

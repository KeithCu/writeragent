from compute_service.json_peel import peel_execute_request
from unittest.mock import patch
import json

def test_peel_skips_unknown_keys():
    payload = b'{"id": "1", "junk": [1,2,3], "code": "1"}'
    original_loads = json.loads
    called_loads_for_junk_value = False
    def mock_loads(s, **kwargs):
        nonlocal called_loads_for_junk_value
        if b"[1,2,3]" in s.encode() or "[1,2,3]" in s:
            called_loads_for_junk_value = True
        return original_loads(s, **kwargs)
    with patch('json.loads', side_effect=mock_loads):
        res = peel_execute_request(payload)
    assert res.req_id == "1"
    assert not called_loads_for_junk_value

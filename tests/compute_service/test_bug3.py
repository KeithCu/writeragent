from compute_service.json_egress import normalize_execute_response

def test_empty_multi_data_result():
    payload = {
        "status": "ok",
        "result": {
            "__wa_payload__": "multi_data",
            "items": []
        }
    }
    out = normalize_execute_response(payload)
    assert out["result"] == []

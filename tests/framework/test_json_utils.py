"""safe_json_loads fallback stages log a name, never the payload."""

from unittest.mock import MagicMock

from plugin.framework.json_utils import safe_json_loads


def test_literal_eval_logs_stage_name_not_payload(monkeypatch) -> None:
    import plugin.framework.json_utils as json_utils

    mock_log = MagicMock()
    monkeypatch.setattr(json_utils, "log", mock_log)
    payload = "{'key': 'secret-literal'}"
    assert safe_json_loads(payload) == {"key": "secret-literal"}
    mock_log.debug.assert_called_once()
    args = mock_log.debug.call_args[0]
    assert "literal_eval" in args
    joined = " ".join(str(part) for part in args)
    assert "secret-literal" not in joined
    assert payload not in joined


def test_repaired_json_logs_stage_name_not_payload(monkeypatch) -> None:
    import plugin.framework.json_utils as json_utils

    mock_log = MagicMock()
    monkeypatch.setattr(json_utils, "log", mock_log)
    payload = '{"key": "secret-repaired"'
    assert safe_json_loads(payload) == {"key": "secret-repaired"}
    mock_log.debug.assert_called_once()
    args = mock_log.debug.call_args[0]
    assert "json_repair" in args
    joined = " ".join(str(part) for part in args)
    assert "secret-repaired" not in joined
    assert payload not in joined


def test_strict_json_does_not_log_a_stage(monkeypatch) -> None:
    import plugin.framework.json_utils as json_utils

    mock_log = MagicMock()
    monkeypatch.setattr(json_utils, "log", mock_log)
    assert safe_json_loads('{"key": "plain"}') == {"key": "plain"}
    mock_log.debug.assert_not_called()

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


def test_safe_json_loads_long_malformed_returns_default() -> None:
    """A body over DEAL_MAX_SOURCE must not raise deal.PreContractError."""
    from plugin.framework.deal_shim import DEAL_MAX_SOURCE
    from plugin.framework.json_utils import repair_json
    from tests.harness.strip_bundle import deal_pre_present

    filler = "x" * (DEAL_MAX_SOURCE + 1)
    payload = '{"a": ' + filler
    if deal_pre_present(repair_json):
        assert safe_json_loads(payload, default="SENTINEL") == "SENTINEL"
        return
    # Stripped bundles drop @deal.pre, so json_repair still closes the string.
    assert safe_json_loads(payload, default="SENTINEL") == {"a": filler}


def test_safe_json_loads_long_valid_json() -> None:
    import json

    payload = json.dumps({"a": "x" * 9000})
    assert safe_json_loads(payload) == {"a": "x" * 9000}


def test_repair_json_object_long_malformed_does_not_raise() -> None:
    from plugin.framework.deal_shim import DEAL_MAX_SOURCE
    from plugin.framework.json_utils import _repair_json_object_bounded, repair_json_object
    from tests.harness.strip_bundle import deal_pre_present

    filler = "x" * (DEAL_MAX_SOURCE + 1)
    payload = '{"a": ' + filler
    if deal_pre_present(_repair_json_object_bounded):
        assert repair_json_object(payload) == payload
        return
    assert repair_json_object(payload) == {"a": filler}


def test_repair_json_object_trailing_comma() -> None:
    from plugin.framework.json_utils import repair_json_object

    assert repair_json_object('{"a": 1,}') == {"a": 1}

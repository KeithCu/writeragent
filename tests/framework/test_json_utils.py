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

    filler = "x" * (DEAL_MAX_SOURCE + 1)
    payload = '{"a": ' + filler
    # Pytest pre is total, so json_repair closes the string the same way a
    # stripped release bundle does. PreContractError must not escape.
    assert safe_json_loads(payload, default="SENTINEL") == {"a": filler}


def test_safe_json_loads_long_valid_json() -> None:
    import json

    payload = json.dumps({"a": "x" * 9000})
    assert safe_json_loads(payload) == {"a": "x" * 9000}


def test_repair_json_object_long_malformed_does_not_raise() -> None:
    from plugin.framework.deal_shim import DEAL_MAX_SOURCE
    from plugin.framework.json_utils import repair_json_object

    filler = "x" * (DEAL_MAX_SOURCE + 1)
    payload = '{"a": ' + filler
    assert repair_json_object(payload) == {"a": filler}


def test_repair_json_object_trailing_comma() -> None:
    from plugin.framework.json_utils import repair_json_object

    assert repair_json_object('{"a": 1,}') == {"a": 1}


def test_safe_json_loads_keeps_json_escapes_that_look_like_latex() -> None:
    """A dotted abbreviation is a newline, not the short LaTeX command.

    What was wrong: ``\\ne`` inside ``\\ne.g.`` was doubled, so json.loads
    kept a literal backslash instead of the newline. The same false word
    boundary applies to ``\\ni.e.`` and to a newline already in the text.
    """
    assert safe_json_loads('{"x": "line\\ne.g. more"}') == {"x": "line\ne.g. more"}
    assert safe_json_loads('{"x": "see\\ni.e. more"}') == {"x": "see\ni.e. more"}
    assert safe_json_loads('{"x": "the\\nu.s. flag"}') == {"x": "the\nu.s. flag"}
    assert safe_json_loads('{"x": "line' + "\n" + 'e.g. more"}') == {"x": "line\ne.g. more"}


def test_safe_json_loads_repairs_bfnrt_latex_clash_commands() -> None:
    """Commands that start with a JSON escape are doubled before loads.

    What was wrong: #1046 skipped every clash word starting with b/f/n/r/t.
    json.loads turned ``\\nabla`` into a newline plus ``abla`` and returned,
    so step 2 never saw a control character.
    """
    assert safe_json_loads('{"a": "\\nabla x"}') == {"a": "\\nabla x"}
    assert safe_json_loads('{"a": "\\times"}') == {"a": "\\times"}
    assert safe_json_loads('{"a": "\\frac{1}{2}"}') == {"a": "\\frac{1}{2}"}
    assert safe_json_loads('{"a": "\\beta"}') == {"a": "\\beta"}
    assert safe_json_loads('{"a": "\\not"}') == {"a": "\\not"}
    assert safe_json_loads('{"a": "\\right"}') == {"a": "\\right"}
    assert safe_json_loads('{"a": "\\alpha"}') == {"a": "\\alpha"}
    # A command that is already escaped must not be doubled again.
    assert safe_json_loads('{"a": "\\\\nabla"}') == {"a": "\\nabla"}


def test_safe_json_loads_repairs_latex_clash_before_subscript_or_digit() -> None:
    """A clash command followed by ``_`` or a digit keeps its backslash.

    What was wrong: ``_LATEX_CLASH_RE`` ended in ``\\b``. ``_`` and digits
    are word characters, so the repair never matched. json.loads then kept
    the control escape and dropped the backslash (``\\beta_i`` became
    backspace + ``eta_i``). A following letter (``\\alphax``) is still not
    the command.
    """
    cases = {
        r'{"x": "\alpha_1"}': "\\alpha_1",
        r'{"x": "\beta_i"}': "\\beta_i",
        r'{"x": "\theta_1"}': "\\theta_1",
        r'{"x": "\nabla_1"}': "\\nabla_1",
        r'{"x": "\frac_1"}': "\\frac_1",
        r'{"x": "\times2"}': "\\times2",
        r'{"x": "\alpha x"}': "\\alpha x",
        r'{"x": "\alpha^2"}': "\\alpha^2",
        r'{"x": "\frac{1}{2}"}': "\\frac{1}{2}",
    }
    for raw, expected in cases.items():
        assert safe_json_loads(raw) == {"x": expected}
    # Already escaped: one backslash in the value, not a second doubling.
    assert safe_json_loads(r'{"x": "\\alpha_1"}') == {"x": "\\alpha_1"}
    # ``\alphax`` is not ``\alpha``. literal_eval keeps the bell from ``\a``.
    assert safe_json_loads(r'{"x": "\alphax"}') == {"x": "\alphax"}

"""safe_json_loads fallback stages log a name, never the payload."""

from unittest.mock import MagicMock

from plugin.framework.json_utils import safe_json_loads
import pytest


@pytest.mark.parametrize(
    "value, value_2, value_3, value_4",
    [
        pytest.param("{'key': 'secret-literal'}", "secret-literal", "literal_eval", "secret-literal", id="test_literal_eval_logs_stage_name_not_payload"),
        pytest.param('{"key": "secret-repaired"', "secret-repaired", "json_repair", "secret-repaired", id="test_repaired_json_logs_stage_name_not_payload"),
    ],
)
def test_literal_eval_logs_stage_name_not_payload(monkeypatch, value, value_2, value_3, value_4) -> None:
    # Release bundles strip log.debug; skip this assertion there.
    from tests.harness.strip_bundle import skip_if_release_build

    skip_if_release_build("log.debug stripped in release bundle")
    import plugin.framework.json_utils as json_utils

    mock_log = MagicMock()
    monkeypatch.setattr(json_utils, "log", mock_log)
    payload = value
    assert safe_json_loads(payload) == {"key": value_2}
    mock_log.debug.assert_called_once()
    args = mock_log.debug.call_args[0]
    assert value_3 in args
    joined = " ".join(str(part) for part in args)
    assert value_4 not in joined
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


def test_safe_json_loads_keeps_json_escapes_that_look_like_latex() -> None:
    """A dotted abbreviation is a newline, not the short LaTeX command.

    ``\\ne`` inside ``\\ne.g.`` must decode as a newline, not double the escape.
    The same word-boundary rule applies to ``\\ni.e.`` and to newlines already
    in the text.
    """
    assert safe_json_loads('{"x": "line\\ne.g. more"}') == {"x": "line\ne.g. more"}
    assert safe_json_loads('{"x": "see\\ni.e. more"}') == {"x": "see\ni.e. more"}
    assert safe_json_loads('{"x": "the\\nu.s. flag"}') == {"x": "the\nu.s. flag"}
    assert safe_json_loads('{"x": "line' + "\n" + 'e.g. more"}') == {"x": "line\ne.g. more"}


def test_safe_json_loads_repairs_bfnrt_latex_clash_commands() -> None:
    """Commands that start with a JSON escape are doubled before loads.

    LaTeX commands that start with a JSON escape (``\\nabla``, ``\\times``, …)
    must be doubled before ``json.loads``. Otherwise loads emits a control
    character and later repair steps never run.
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

    ``_LATEX_CLASH_RE`` must match clash commands before ``_`` or digits
    (word characters after ``\\b``). Without that, ``json.loads`` turns
    ``\\beta_i`` into a control character plus ``eta_i``.
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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("True", True),
        ("None", None),
        ("'hi'", "hi"),
        ("{'a': 0x10}", {"a": 16}),
    ],
)
def test_python_literals_parse_in_the_literal_eval_stage(text: str, expected: object) -> None:
    assert safe_json_loads(text, default="D") == expected


def test_literal_eval_stage_rejects_non_json_types() -> None:
    assert not isinstance(safe_json_loads("(1, 2)", default="d"), tuple)
    assert safe_json_loads("b'hi'", default="d") == "d"

# WriterAgent - tests for web_search HITL query override coercion
from types import SimpleNamespace

from plugin.modules.chatbot.web_research import (
    _apply_web_search_query_override,
    _web_search_query_from_arguments,
)


def test_web_search_query_from_dict():
    assert _web_search_query_from_arguments({"query": "hello"}) == "hello"
    assert _web_search_query_from_arguments({}) == ""


def test_web_search_query_from_json_string():
    assert _web_search_query_from_arguments('{"query": "from json"}') == "from json"


def test_web_search_query_from_invalid_string():
    assert _web_search_query_from_arguments("not json") == ""
    assert _web_search_query_from_arguments(None) == ""


def test_apply_override_dict_mutable():
    step = SimpleNamespace(arguments={"query": "old", "x": 1})
    assert _apply_web_search_query_override(step, "new") is False
    assert step.arguments == {"query": "new", "x": 1}


def test_apply_override_json_string():
    step = SimpleNamespace(arguments='{"query": "old"}')
    assert _apply_web_search_query_override(step, "edited") is True
    assert step.arguments == {"query": "edited"}


def test_apply_override_invalid_json_string():
    step = SimpleNamespace(arguments="<<<")
    assert _apply_web_search_query_override(step, "only-override") is True
    assert step.arguments == {"query": "only-override"}


def test_apply_override_non_dict_non_string():
    step = SimpleNamespace(arguments=["list"])
    assert _apply_web_search_query_override(step, "fallback") is True
    assert step.arguments == {"query": "fallback"}

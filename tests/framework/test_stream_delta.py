import pytest

from plugin.framework.stream_delta import accumulate_delta, coalesce_split_tool_calls


def test_accumulate_delta_simple():
    acc = {"a": "hello"}
    delta = {"a": " world"}
    result = accumulate_delta(acc, delta)
    assert result == {"a": "hello world"}
    assert acc is result


def test_accumulate_delta_new_key():
    acc = {"a": "hello"}
    delta = {"b": 42}
    result = accumulate_delta(acc, delta)
    assert result == {"a": "hello", "b": 42}


def test_accumulate_delta_null_base():
    acc = {"a": None}
    delta = {"a": "value"}
    result = accumulate_delta(acc, delta)
    assert result == {"a": "value"}


def test_accumulate_delta_special_keys():
    acc = {"index": 1, "type": "old"}
    delta = {"index": 2, "type": "new"}
    result = accumulate_delta(acc, delta)
    assert result == {"index": 2, "type": "new"}


def test_accumulate_delta_numeric():
    acc = {"num": 10, "float": 1.5}
    delta = {"num": 5, "float": 2.5}
    result = accumulate_delta(acc, delta)
    assert result == {"num": 15, "float": 4.0}


def test_accumulate_delta_nested_dict():
    acc = {"obj": {"x": "a"}}
    delta = {"obj": {"y": "b", "x": "c"}}
    result = accumulate_delta(acc, delta)
    assert result == {"obj": {"x": "ac", "y": "b"}}


def test_accumulate_delta_list_simple():
    acc = {"list": ["a", 1]}
    delta = {"list": ["b", 2]}
    result = accumulate_delta(acc, delta)
    assert result == {"list": ["a", 1, "b", 2]}


def test_accumulate_delta_list_objects():
    acc = {"items": []}
    delta = {"items": [{"index": 0, "val": "a"}]}
    result = accumulate_delta(acc, delta)
    assert result == {"items": [{"index": 0, "val": "a"}]}

    delta2 = {"items": [{"index": 0, "val": "b"}]}
    result2 = accumulate_delta(result, delta2)
    assert result2 == {"items": [{"index": 0, "val": "ab"}]}

    delta3 = {"items": [{"index": 1, "val": "c"}]}
    result3 = accumulate_delta(result2, delta3)
    assert result3 == {"items": [{"index": 0, "val": "ab"}, {"index": 1, "val": "c"}]}


def test_accumulate_delta_errors():
    acc = {"items": [{"index": 0, "val": "a"}]}

    # Missing index
    with pytest.raises(RuntimeError):
        accumulate_delta(acc, {"items": [{"val": "b"}]})

    # Bad index type
    with pytest.raises(TypeError):
        accumulate_delta(acc, {"items": [{"index": "0", "val": "b"}]})

    # Non-dict delta entry
    with pytest.raises(TypeError):
        accumulate_delta(acc, {"items": ["bad"]})


def test_accumulate_delta_rejects_non_plain_dict():
    """Mapping subclasses that isinstance(dict) must be rejected (plain dict only)."""
    from collections import UserDict

    from tests.harness.strip_bundle import expect_pre_or_body

    expect_pre_or_body(
        lambda: accumulate_delta(UserDict({"a": 1}), {"a": 2}),  # type: ignore[arg-type]
        body_exc=TypeError,
    )
    expect_pre_or_body(
        lambda: accumulate_delta({"a": 1}, UserDict({"a": 2})),  # type: ignore[arg-type]
        body_exc=TypeError,
    )


def test_coalesce_split_tool_calls_merges_empty_name_continuation():
    tool_calls = [
        {
            "index": 0,
            "id": "chatcmpl-tool-abc",
            "type": "function",
            "function": {
                "name": "delegate_to_specialized_writer_toolset",
                "arguments": '{"message": "back to the Writer',
            },
        },
        {
            "index": 1,
            "id": "",
            "type": "function",
            "function": {"name": "", "arguments": ' sidebar."\n}'},
        },
    ]
    out = coalesce_split_tool_calls(tool_calls)
    assert len(out) == 1
    assert out[0]["id"] == "chatcmpl-tool-abc"
    assert out[0]["function"]["name"] == "delegate_to_specialized_writer_toolset"
    assert out[0]["function"]["arguments"] == '{"message": "back to the Writer sidebar."\n}'
    assert out[0]["index"] == 0


def test_coalesce_split_tool_calls_merges_cerebras_lookup_stream_split():
    # Same OpenRouter/Cerebras gpt-oss shape as the client stream fixture:
    # new index, empty id/name, remainder of arguments.
    out = coalesce_split_tool_calls(
        [
            {
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"query":"part'},
            },
            {
                "index": 1,
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ' two"}'},
            },
        ]
    )
    assert len(out) == 1
    assert out[0]["id"] == "call_1"
    assert out[0]["function"]["name"] == "lookup"
    assert out[0]["function"]["arguments"] == '{"query":"part two"}'
    assert out[0]["index"] == 0


def test_coalesce_split_tool_calls_lone_empty_name_dropped():
    assert coalesce_split_tool_calls([
        {"index": 0, "id": "", "function": {"name": "", "arguments": " orphan"}},
    ]) == []
    assert coalesce_split_tool_calls(None) == []
    assert coalesce_split_tool_calls([]) == []

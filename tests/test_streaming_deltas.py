import pytest
from plugin.framework.streaming_deltas import accumulate_delta

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

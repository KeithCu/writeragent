from __future__ import annotations

import datetime
import decimal
import fractions
import math
from typing import Any

import pytest

from plugin.framework.ipc import HOST_PICKLE_LEAF_TYPES
from plugin.scripting.payload_codec import PAYLOAD_DATAFRAME, is_image_payload, is_split_grid
from plugin.scripting.venv.to_wire import to_wire


def test_pickle_scalars_and_leaves():
    """Pickle preserves float nan, bytes, sets; drops tz; rejects sNaN."""
    nan_val = to_wire(float("nan"), target="pickle")
    assert isinstance(nan_val, float)
    assert math.isnan(nan_val)

    assert to_wire(b"ab", target="pickle") == b"ab"
    assert to_wire(bytearray(b"ab"), target="pickle") == bytearray(b"ab")
    assert to_wire({1}, target="pickle") == {1}
    assert to_wire(frozenset({1}), target="pickle") == frozenset({1})

    dt = datetime.datetime(2026, 8, 13, 14, 30, tzinfo=datetime.timezone.utc)
    assert to_wire(dt, target="pickle") == "2026-08-13T14:30:00"

    with pytest.raises(ValueError):
        to_wire(decimal.Decimal("sNaN"), target="pickle")


def test_pickle_dataframe_depth_nine():
    """Pickle converts DataFrame nested 9 or more lists deep (behaviour change from depth cap 8)."""
    pd = pytest.importorskip("pandas")
    df = pd.DataFrame({"a": [1, 2]})

    cur: Any = df
    for _ in range(9):
        cur = [cur]

    res = to_wire(cur, target="pickle")
    node = res
    for _ in range(9):
        assert isinstance(node, list)
        assert len(node) == 1
        node = node[0]

    assert isinstance(node, dict)
    assert node.get("__wa_payload__") == PAYLOAD_DATAFRAME
    assert node.get("columns") == ["a"]


def test_json_behaviour_changes():
    """JSON target coerces temporals/Decimals, base64-encodes bytes, and preserves types."""
    # Dates, temporals, Decimals match pickle coercions
    d = datetime.date(2026, 8, 13)
    assert to_wire(d, target="json") == "2026-08-13"

    dt = datetime.datetime(2026, 8, 13, 14, 30, tzinfo=datetime.timezone.utc)
    assert to_wire(dt, target="json") == "2026-08-13T14:30:00"

    t = datetime.time(14, 30)
    assert to_wire(t, target="json") == "14:30:00"

    td = datetime.timedelta(days=1)
    assert to_wire(td, target="json") == 1.0

    dec = decimal.Decimal("1.5")
    assert to_wire(dec, target="json") == 1.5

    frac = fractions.Fraction(1, 4)
    assert to_wire(frac, target="json") == 0.25

    rng = range(3)
    assert to_wire(rng, target="json") == [0, 1, 2]

    # Non-finite floats and Decimals become None
    assert to_wire(decimal.Decimal("NaN"), target="json") is None
    assert to_wire(decimal.Decimal("Infinity"), target="json") is None
    assert to_wire(decimal.Decimal("-Infinity"), target="json") is None
    assert to_wire(float("nan"), target="json") is None
    assert to_wire(float("inf"), target="json") is None
    assert to_wire(float("-inf"), target="json") is None

    with pytest.raises(ValueError):
        to_wire(decimal.Decimal("sNaN"), target="json")

    # Bytes and bytearray become base64
    assert to_wire(b"ab", target="json") == "YWI="
    assert to_wire(bytearray(b"ab"), target="json") == "YWI="

    # Sets become lists in iteration order
    s = {1, 2}
    res_s = to_wire(s, target="json")
    assert isinstance(res_s, list)
    assert sorted(res_s) == [1, 2]

    # Booleans stay bool
    assert to_wire(True, target="json") is True
    assert type(to_wire(True, target="json")) is bool
    assert to_wire(False, target="json") is False
    assert type(to_wire(False, target="json")) is bool

    # Dict key collision raises
    with pytest.raises(ValueError, match="dict keys collide"):
        to_wire({1: "a", "1": "b"}, target="json")
    assert to_wire({1: "a"}, target="json") == {"1": "a"}

    # Complex stays complex
    c = 1 + 2j
    assert to_wire(c, target="json") == c

    # 200-int vector is a list of ints, not split_grid
    vec = list(range(200))
    res_vec_pickle = to_wire(vec, target="pickle")
    assert is_split_grid(res_vec_pickle)
    res_vec_json = to_wire(vec, target="json")
    assert isinstance(res_vec_json, list)
    assert not is_split_grid(res_vec_json)
    assert res_vec_json == vec

    # Figure payload data is base64 str and keys are not data_b64
    plt = pytest.importorskip("matplotlib.pyplot")
    fig, ax = plt.subplots()
    ax.plot([1, 2, 3])
    fig_json = to_wire(fig, target="json")
    plt.close(fig)

    assert fig_json["__wa_payload__"] == "image"
    assert fig_json["format"] == "svg"
    assert "data_b64" not in fig_json
    assert isinstance(fig_json["data"], str)
    assert not is_image_payload(fig_json)  # false because data is str, not bytes


def test_figure_svg_bytes_are_stable_across_renders():
    """Rendering the same figure twice gives identical SVG bytes, so the Calc digest cache hits."""
    plt = pytest.importorskip("matplotlib.pyplot")
    import hashlib

    from plugin.scripting.venv.to_wire import _figure_to_image_payload

    fig, ax = plt.subplots()
    ax.plot([1, 2, 3])
    first = _figure_to_image_payload(fig)["data"]
    second = _figure_to_image_payload(fig)["data"]
    plt.close(fig)

    # Revert target: metadata={"Date": None} and the fixed svg.hashsalt in _figure_to_image_payload
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()


def test_leaf_assertion_on_pickle_result():
    """Walk a pickle result and verify every leaf is in HOST_PICKLE_LEAF_TYPES and dict keys are str."""
    pd = pytest.importorskip("pandas")
    sample_df = pd.DataFrame({"val": [1, 2]})

    data = {
        "date": datetime.date(2026, 8, 13),
        "dec": decimal.Decimal("2.5"),
        "df": sample_df,
        "set": {10, 20},
        "bytes": b"xyz",
    }

    result = to_wire(data, target="pickle")

    def assert_leaves(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                assert type(k) is str
                assert_leaves(v)
        elif isinstance(node, (list, tuple, set, frozenset)):
            for item in node:
                assert_leaves(item)
        else:
            assert isinstance(node, HOST_PICKLE_LEAF_TYPES), f"Leaf {node!r} of type {type(node)} not in HOST_PICKLE_LEAF_TYPES"

    assert_leaves(result)


def test_errors_and_depth_limits():
    """Depth caps, cycle detection, clongdouble, and unknown class behave identically across targets."""
    class Unknown:
        pass

    for target in ("pickle", "json"):
        with pytest.raises(ValueError, match="cannot cross the LibreOffice pickle boundary"):
            to_wire(Unknown(), target=target)

        # Deep nesting (> 64) raises
        cur: list[Any] = []
        root = cur
        for _ in range(80):
            nxt: list[Any] = []
            cur.append(nxt)
            cur = nxt
        with pytest.raises(ValueError, match="too deeply nested"):
            to_wire(root, target=target)

        # Cycle raises
        cycle: list[Any] = []
        cycle.append(cycle)
        with pytest.raises(ValueError, match="self-referential container"):
            to_wire(cycle, target=target)

    np = pytest.importorskip("numpy")
    for target in ("pickle", "json"):
        with pytest.raises(ValueError, match="clongdouble"):
            to_wire(np.clongdouble(1 + 2j), target=target)

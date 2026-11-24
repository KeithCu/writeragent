from __future__ import annotations

import datetime
import decimal
import math
from typing import Any

import pytest

from plugin.framework.ipc import HOST_PICKLE_LEAF_TYPES
from plugin.scripting.payload_codec import PAYLOAD_DATAFRAME, is_split_grid
from plugin.scripting.venv.to_wire import to_wire


def test_pickle_scalars_and_leaves():
    """Pickle preserves float nan, bytes, sets; drops tz; rejects sNaN; packs large lists."""
    nan_val = to_wire(float("nan"))
    assert isinstance(nan_val, float)
    assert math.isnan(nan_val)

    assert to_wire(b"ab") == b"ab"
    assert to_wire(bytearray(b"ab")) == bytearray(b"ab")
    assert to_wire({1}) == {1}
    assert to_wire(frozenset({1})) == frozenset({1})

    dt = datetime.datetime(2026, 8, 13, 14, 30, tzinfo=datetime.timezone.utc)
    assert to_wire(dt) == "2026-08-13T14:30:00"

    assert is_split_grid(to_wire(list(range(200))))

    with pytest.raises(ValueError):
        to_wire(decimal.Decimal("sNaN"))


def test_pickle_numpy_scalar_subclasses_become_builtins():
    import pickle

    np = pytest.importorskip("numpy")
    samples = [
        np.str_("hello"),
        [np.str_("nested")],
        {np.str_("in_set")},
        {"k": np.str_("val")},
        np.bytes_(b"raw"),
        np.complex128(1 + 2j),
    ]
    for sample in samples:
        wired = to_wire(sample)
        assert b"numpy" not in pickle.dumps(wired, protocol=5)
    assert type(to_wire(np.str_("hello"))) is str
    assert type(to_wire(np.bytes_(b"raw"))) is bytes
    assert type(to_wire(np.complex128(1 + 2j))) is complex
    assert to_wire(np.complex64(complex("nan"))) is None


def test_pickle_dataframe_depth_nine():
    """Pickle converts DataFrame nested 9 or more lists deep (behaviour change from depth cap 8)."""
    pd = pytest.importorskip("pandas")
    df = pd.DataFrame({"a": [1, 2]})

    cur: Any = df
    for _ in range(9):
        cur = [cur]

    res = to_wire(cur)
    node = res
    for _ in range(9):
        assert isinstance(node, list)
        assert len(node) == 1
        node = node[0]

    assert isinstance(node, dict)
    assert node.get("__wa_payload__") == PAYLOAD_DATAFRAME
    assert node.get("columns") == ["a"]


def test_figure_svg_bytes_are_stable_across_renders():
    """Rendering the same figure twice gives identical SVG bytes, so the Calc digest cache hits."""
    plt = pytest.importorskip("matplotlib.pyplot")
    import hashlib

    from plugin.scripting.venv.to_wire import figure_to_image_payload

    fig, ax = plt.subplots()
    ax.plot([1, 2, 3])
    first = figure_to_image_payload(fig)["data"]
    second = figure_to_image_payload(fig)["data"]
    plt.close(fig)

    # Revert target: metadata={"Date": None} and the fixed svg.hashsalt in figure_to_image_payload
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

    result = to_wire(data)

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
    """Depth caps, cycle detection, clongdouble, and unknown class raise ValueError."""
    class Unknown:
        pass

    with pytest.raises(ValueError, match="cannot cross the LibreOffice pickle boundary"):
        to_wire(Unknown())

    # Deep nesting (> 64) raises
    cur: list[Any] = []
    root = cur
    for _ in range(80):
        nxt: list[Any] = []
        cur.append(nxt)
        cur = nxt
    with pytest.raises(ValueError, match="too deeply nested"):
        to_wire(root)

    # Cycle raises
    cycle: list[Any] = []
    cycle.append(cycle)
    with pytest.raises(ValueError, match="self-referential container"):
        to_wire(cycle)

    np = pytest.importorskip("numpy")
    with pytest.raises(ValueError, match="clongdouble"):
        to_wire(np.clongdouble(1 + 2j))


def test_frame_and_series_fallback_grids():
    """DataFrame and Series fallback paths convert object and non-numeric frames to wire format."""
    pd = pytest.importorskip("pandas")

    class RaisingDataFrame(pd.DataFrame):
        @property
        def _constructor(self):
            return RaisingDataFrame

        def to_numpy(self, *args, **kwargs):
            raise RuntimeError("to_numpy failed")

    class RaisingSeries(pd.Series):
        @property
        def _constructor(self):
            return RaisingSeries

        def to_numpy(self, *args, **kwargs):
            raise RuntimeError("to_numpy failed")

    df_raising = RaisingDataFrame(
        {"a": [1, 2], "b": [pd.Timestamp("2024-01-02"), None]}
    )
    wire_df = to_wire(df_raising)
    assert wire_df == {
        "__wa_payload__": "dataframe",
        "columns": ["a", "b"],
        "data": [[1, "2024-01-02T00:00:00"], [2, None]],
    }

    df_obj = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    wire_obj = to_wire(df_obj)
    assert wire_obj == {
        "__wa_payload__": "dataframe",
        "columns": ["a", "b"],
        "data": [[1, "x"], [2, "y"]],
    }

    s_raising = RaisingSeries([1.5, 2.5], name="s")
    wire_s = to_wire(s_raising)
    assert wire_s == {
        "__wa_payload__": "dataframe",
        "columns": ["s"],
        "data": [1.5, 2.5],
    }


def test_to_wire_nat_becomes_none():
    pd = pytest.importorskip("pandas")

    assert to_wire(pd.NaT) is None
    assert to_wire([pd.NA, pd.NaT]) == [None, None]
    assert to_wire(pd.Timedelta("NaT")) is None
    assert to_wire([[pd.NaT, 1]]) == [[None, 1]]
    assert to_wire(pd.Timestamp("2024-01-02")) == "2024-01-02T00:00:00"

    s_unnamed = pd.Series([1.5, 2.5])
    wire_unnamed = to_wire(s_unnamed)
    assert wire_unnamed == [1.5, 2.5]


def test_default_range_index_frame_has_no_header():
    pd = pytest.importorskip("pandas")
    from plugin.scripting.payload_codec import is_dataframe_payload
    from plugin.scripting.venv.to_wire import to_wire

    df_default = pd.DataFrame([["h1", "h2"], [1, 2]])
    wire_default = to_wire(df_default)
    assert not is_dataframe_payload(wire_default)
    assert wire_default == [["h1", "h2"], [1, 2]]

    # List labels stay a plain Index, so they are real headers. pandas 3 makes
    # dict keys 0..N-1 a RangeIndex, which is indistinguishable from default labels.
    df_explicit_int = pd.DataFrame([[1, 2]], columns=[0, 1])
    wire_explicit = to_wire(df_explicit_int)
    assert is_dataframe_payload(wire_explicit)

    df_empty = pd.DataFrame(columns=pd.RangeIndex(0, 2))
    assert to_wire(df_empty) == []


def test_dense_plot_svg_falls_back_to_png():
    mpl = pytest.importorskip("matplotlib")
    mpl.use("Agg")
    import matplotlib.pyplot as plt
    np = pytest.importorskip("numpy")
    from plugin.scripting.venv.to_wire import figure_to_image_payload, plot_image_payload

    rng = np.random.default_rng(0)
    fig_dense, ax_dense = plt.subplots()
    try:
        ax_dense.scatter(rng.normal(size=20_000), rng.normal(size=20_000), s=1)
        dense_payload = plot_image_payload(fig_dense)
        assert dense_payload["format"] == "png"
        assert dense_payload["data"].startswith(b"\x89PNG")
        svg_payload = figure_to_image_payload(fig_dense)
        assert len(dense_payload["data"]) < len(svg_payload["data"])

        wire_dense = to_wire({"plot": fig_dense})
        assert wire_dense["plot"]["format"] == "png"
    finally:
        plt.close(fig_dense)

    fig_small, ax_small = plt.subplots()
    try:
        ax_small.plot([1, 2, 3], [1, 4, 9])
        assert plot_image_payload(fig_small)["format"] == "svg"
    finally:
        plt.close(fig_small)

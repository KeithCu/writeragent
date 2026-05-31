# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Shared fixtures, oracles, and venv round-trip harness for A/B serialization tests."""

from __future__ import annotations

import math
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable

import hypothesis.strategies as st
from hypothesis import strategies

import plugin.scripting.payload_codec as pc
from plugin.calc.calc_addin_data import normalize_python_data_shape
from plugin.scripting.payload_codec import (
    BINARY_MIN_CELLS,
    ForceBinary,
    child_unpack_data,
    host_pack_data,
    host_pack_multi_data,
    host_unpack_data,
    is_multi_data,
    is_numeric_grid,
    is_split_grid,
    fast_flatten_grid_2d,
)


@contextmanager
def cython_accelerator_context(enabled: bool):
    """Context manager to temporarily enable/disable the Cython accelerator."""
    orig_2d = pc.fast_flatten_grid_2d
    orig_1d = pc.fast_flatten_grid_1d
    if not enabled:
        pc.fast_flatten_grid_2d = None
        pc.fast_flatten_grid_1d = None
    try:
        yield
    finally:
        pc.fast_flatten_grid_2d = orig_2d
        pc.fast_flatten_grid_1d = orig_1d


def assert_cython_vs_python_parity(
    grid: list[Any] | list[list[Any]],
    code: str,
    *,
    label: str = "",
) -> None:
    """Run worker code twice: once with Cython and once with Pure Python. Assert parity."""
    if pc.fast_flatten_grid_2d is None and pc.fast_flatten_grid_1d is None:
        # Skip if Cython not available on this platform/build
        return

    with cython_accelerator_context(enabled=True):
        result_cython = run_venv_roundtrip(grid, code, pack_force="always")

    with cython_accelerator_context(enabled=False):
        result_python = run_venv_roundtrip(grid, code, pack_force="always")

    assert_semantically_equal(result_cython, result_python, label=f"{label} (Cython vs Python)")
from plugin.scripting.venv_worker import PythonWorkerManager
from plugin.scripting.worker_harness import _execute_request
from tests.calc.serialization_cases import SerializationCase, all_serialization_cases
from tests.scripting.payload_codec_test_support import (
    MIXED_LABEL_GRID,
    MIXED_WITH_ZIP,
    NUMERIC_4X4,
    grid_with_cell_count,
    sequential_grid_sum,
)

# Worker code strings (assign to result — matches test_run_venv_code.py style).
VENV_CODE_ECHO = "result = data"
VENV_CODE_SUM = "result = float(np.sum(data))"
VENV_CODE_DOUBLE = "result = (np.asarray(data) * 2).tolist()"
VENV_CODE_NANSUM = "result = float(np.nansum(data))"
VENV_CODE_MIXED_SUM = (
    "result = sum(v for row in (data if (isinstance(data, list) and data and "
    "isinstance(data[0], list)) else [data]) for v in row if isinstance(v, (int, float)))"
)
VENV_CODE_MULTI_SUM = "result = sum(np.sum(d) for d in data_list)"
VENV_CODE_MULTI_MIXED_SUM = (
    "result = float(sum("
    "v for g in data_list "
    "for row in (g.tolist() if hasattr(g, 'shape') else "
    "(g if isinstance(g, list) and g and isinstance(g[0], (list, tuple)) else [g if isinstance(g, list) else [g]])) "
    "for v in (row if isinstance(row, (list, tuple)) else [row]) "
    "if isinstance(v, (int, float))"
    "))"
)

VENV_TRANSFORMS: dict[str, str] = {
    "echo": VENV_CODE_ECHO,
    "sum": VENV_CODE_SUM,
    "double": VENV_CODE_DOUBLE,
    "nansum": VENV_CODE_NANSUM,
    "mixed_sum": VENV_CODE_MIXED_SUM,
}

_WORKER_ENV = {"PATH": "/usr/bin:/bin"}


@dataclass(frozen=True)
class AbGridCase:
    id: str
    grid: list[Any] | list[list[Any]]
    tags: frozenset[str] = frozenset()
    calc_shape: bool = False
    expect_split_auto: bool | None = None
    venv_code: str | None = None
    expected: Any = None
    grid_b: list[Any] | list[list[Any]] | None = None


@dataclass(frozen=True)
class VenvTransformCase:
    id: str
    grid: list[Any] | list[list[Any]]
    code: str
    expected: Any | None = None
    tags: frozenset[str] = field(default_factory=frozenset)
    use_subprocess: bool = False
    grid_b: list[Any] | list[list[Any]] | None = None


def _make_numeric_grid(rows: int, cols: int) -> list[list[float]]:
    return [[float(r * cols + c + 1) for c in range(cols)] for r in range(rows)]


def _expect_split_auto(grid: list[Any] | list[list[Any]]) -> bool:
    return grid_cell_count(grid) >= BINARY_MIN_CELLS


def _ab_grid_case(
    case_id: str,
    grid: list[Any] | list[list[Any]],
    tags: frozenset[str],
    *,
    expect_split_auto: bool | None = None,
    **kwargs: Any,
) -> AbGridCase:
    exp = _expect_split_auto(grid) if expect_split_auto is None else expect_split_auto
    return AbGridCase(case_id, grid, tags, expect_split_auto=exp, **kwargs)


def _make_mixed_grid() -> list[list[Any]]:
    return [
        [1, "hello", 3.14, True, None],
        [100, "world", 2.71, False, 42],
        [999, "02138", -1.0, True, math.nan],
    ]


def _make_edge_case_grid() -> list[list[Any]]:
    return [
        [0, 1, -1, 1.0, -1.0],
        [True, False, None, "", "   "],
        [1e10, 1e-10, float("inf"), float("-inf"), 0.0],
        ["café", "日本語", "emoji🎉", "zip:90210", "long string value here"],
    ]


def _make_single_column_ints(n: int = 11) -> list[list[int]]:
    return [[i] for i in range(n)]


def _make_1d_mixed() -> list[Any]:
    return [10, "text", 3.14, None, True, "02138", 0, False, 1.5, 2.5, 3.5]


def _make_bool_column_grid() -> list[list[Any]]:
    return [
        [True, "apple", 10],
        [False, "banana", 20],
        [True, "cherry", None],
        [None, "date", 40],
    ]


def _make_logical_string_grid() -> list[list[Any]]:
    return [["TRUE", "FALSCH"], ["banana", 100.0]]


def _make_numpy_scalar_grid() -> list[list[Any]]:
    import numpy as np

    return [[np.float64(1.5), np.int64(7)], [np.float64(2.5), np.int64(8)]]


def _case_to_ab(case: SerializationCase) -> AbGridCase | None:
    if case.sheet == "errors" or case.mode == "error":
        return None
    if not case.input_grid and case.id != "scalar_single_cell":
        return None
    calc_shape = "flat" in case.tags or case.id in ("scalar_row_sum", "scalar_col_sum", "bool_col_11_sum")
    expect_split = None
    ncells = grid_cell_count(list(case.input_grid) if case.input_grid else [[42.0]])
    if case.input_grid_b is not None:
        ncells += grid_cell_count(list(case.input_grid_b))
    if ncells >= BINARY_MIN_CELLS:
        expect_split = True
    elif ncells > 0:
        expect_split = False
    venv_code = _serialization_code_to_venv(case.code)
    return AbGridCase(
        id=f"case_{case.id}",
        grid=list(case.input_grid) if case.input_grid else [[42.0]],
        grid_b=list(case.input_grid_b) if case.input_grid_b is not None else None,
        tags=frozenset(case.tags),
        calc_shape=calc_shape,
        expect_split_auto=expect_split,
        venv_code=venv_code,
        expected=case.expected,
    )


def _serialization_code_to_venv(code: str) -> str:
    stripped = code.strip()
    if stripped.startswith("result"):
        return stripped
    if stripped == "data":
        return VENV_CODE_ECHO
    if stripped == "np.sum(data)":
        return VENV_CODE_SUM
    if stripped == "np.max(data)":
        return "result = float(np.max(data))"
    if stripped == "np.nansum(data)":
        return VENV_CODE_NANSUM
    if stripped == "np.array(data) * 2":
        return VENV_CODE_DOUBLE
    if "for g in data" in stripped and "sum(v for" in stripped:
        return VENV_CODE_MULTI_MIXED_SUM
    if "sum(v for row" in stripped:
        return VENV_CODE_MIXED_SUM
    if stripped == "sum(np.sum(d) for d in data)":
        return VENV_CODE_MULTI_SUM
    if stripped == "data[0]":
        return "result = data[0] if not isinstance(data[0], list) else data[0][0]"
    return f"result = {stripped}"


def _builtin_ab_cases() -> list[AbGridCase]:
    n = BINARY_MIN_CELLS
    row_at = [[float(i + 1) for i in range(n)]]
    col_at = [[float(i + 1)] for i in range(n)]
    return [
        _ab_grid_case("small_numeric_3x3", _make_numeric_grid(3, 3), frozenset({"below_threshold"}), expect_split_auto=False),
        _ab_grid_case("numeric_4x4", NUMERIC_4X4, frozenset({"split_grid"}), venv_code=VENV_CODE_SUM, expected=264.0),
        _ab_grid_case("mixed_label", MIXED_LABEL_GRID, frozenset({"mixed", "split_grid"})),
        _ab_grid_case("mixed_zip", MIXED_WITH_ZIP, frozenset({"mixed", "zip_code", "split_grid"})),
        _ab_grid_case("mixed_complex", _make_mixed_grid(), frozenset({"mixed", "split_grid"})),
        _ab_grid_case("edge_cases", _make_edge_case_grid(), frozenset({"mixed", "unicode", "split_grid"})),
        _ab_grid_case("single_col_ints_11", _make_single_column_ints(11), frozenset({"int", "split_grid", "flat"}), calc_shape=True),
        _ab_grid_case("flat_1d_mixed", _make_1d_mixed(), frozenset({"mixed", "flat", "split_grid"})),
        _ab_grid_case("tiny_below_threshold", [[1, 2], [3, 4]], frozenset({"below_threshold"}), expect_split_auto=False),
        _ab_grid_case("exactly_threshold", grid_with_cell_count(n), frozenset({"boundary", "split_grid"}), expect_split_auto=True),
        _ab_grid_case("one_below_threshold", grid_with_cell_count(max(1, n - 1)), frozenset({"below_threshold"}), expect_split_auto=False),
        _ab_grid_case("one_above_threshold", grid_with_cell_count(n + 1), frozenset({"split_grid"}), expect_split_auto=True),
        _ab_grid_case("all_bools", [[True, False], [False, True]], frozenset({"bool", "below_threshold"}), expect_split_auto=False),
        _ab_grid_case("int_float_mixed_cols", [[1, 2.5], [3, 4.0], [5, 6.5]], frozenset({"below_threshold"}), expect_split_auto=False),
        _ab_grid_case("with_inf_nan", [[1.0, float("inf")], [float("-inf"), math.nan]], frozenset({"below_threshold"}), expect_split_auto=False),
        _ab_grid_case("unicode_strings", [["café", "日本語"], ["emoji🎉", "test"]], frozenset({"unicode", "below_threshold"}), expect_split_auto=False),
        _ab_grid_case("bool_column_mixed", _make_bool_column_grid(), frozenset({"bool", "mixed", "split_grid"})),
        _ab_grid_case("logical_strings", _make_logical_string_grid(), frozenset({"bool", "below_threshold"}), expect_split_auto=False),
        _ab_grid_case("numpy_scalars", _make_numpy_scalar_grid(), frozenset({"below_threshold"}), expect_split_auto=False),
        _ab_grid_case(
            "row_at_threshold",
            row_at,
            frozenset({"flat", "split_grid"}),
            calc_shape=True,
            expect_split_auto=True,
            venv_code=VENV_CODE_SUM,
            expected=sequential_grid_sum(n),
        ),
        _ab_grid_case(
            "col_at_threshold",
            col_at,
            frozenset({"flat", "split_grid"}),
            calc_shape=True,
            expect_split_auto=True,
            venv_code=VENV_CODE_SUM,
            expected=sequential_grid_sum(n),
        ),
    ]


def all_ab_grid_cases() -> list[AbGridCase]:
    """Named fixture corpus for parametrized A/B tests."""
    seen: set[str] = set()
    out: list[AbGridCase] = []
    for case in _builtin_ab_cases():
        if case.id not in seen:
            seen.add(case.id)
            out.append(case)
    for sc in all_serialization_cases():
        ab = _case_to_ab(sc)
        if ab is not None and ab.id not in seen:
            seen.add(ab.id)
            out.append(ab)
    return out


def prepare_grid(case: AbGridCase) -> list[Any] | list[list[Any]]:
    grid = case.grid
    if case.calc_shape and grid and isinstance(grid[0], list):
        shaped = normalize_python_data_shape([list(row) for row in grid])
        return shaped
    return grid


def prepare_multi_grids(case: AbGridCase) -> tuple[list[Any] | list[list[Any]], list[Any] | list[list[Any]]] | None:
    """Return shaped (grid_a, grid_b) when *case* is a multi-range fixture."""
    if case.grid_b is None:
        return None
    grid_a = prepare_grid(case)
    grid_b_case = AbGridCase(id=case.id, grid=case.grid_b, calc_shape=case.calc_shape, tags=case.tags)
    grid_b = prepare_grid(grid_b_case)
    return grid_a, grid_b


def grid_cell_count(grid: list[Any] | list[list[Any]]) -> int:
    if not grid:
        return 0
    if isinstance(grid[0], (list, tuple)):
        return sum(len(row) for row in grid)
    return len(grid)


def codec_child_materialization(grid: list[Any] | list[list[Any]], *, force: ForceBinary) -> Any:
    return child_unpack_data(host_pack_data(grid, force=force))


def codec_host_decode(grid: list[Any] | list[list[Any]], *, force: ForceBinary) -> Any:
    """Host-side decode only (no venv): split_grid envelope vs nested list wire."""
    wire = host_pack_data(grid, force=force)
    return host_unpack_data(wire, as_nested_list=True)


def flatten_semantic_cells(value: Any) -> list[Any]:
    """Flatten nested lists/ndarrays to leaf cells for cross-wire-shape comparison."""
    import numpy as np

    if isinstance(value, np.ndarray):
        value = value.tolist()
    if isinstance(value, (list, tuple)):
        out: list[Any] = []
        for item in value:
            out.extend(flatten_semantic_cells(item))
        return out
    return [normalize_for_oracle(value)]


def assert_codec_split_vs_nosplit_parity(
    grid: list[Any] | list[list[Any]],
    *,
    label: str,
) -> None:
    """force=always (split_grid) vs force=never (nested list) must decode equivalently."""
    child_split = codec_child_materialization(grid, force="always")
    child_nosplit = codec_child_materialization(grid, force="never")
    assert flatten_semantic_cells(child_split) == flatten_semantic_cells(child_nosplit), (
        f"{label} child decode: {child_split!r} vs {child_nosplit!r}"
    )

    host_split = codec_host_decode(grid, force="always")
    host_nosplit = codec_host_decode(grid, force="never")
    assert flatten_semantic_cells(host_split) == flatten_semantic_cells(host_nosplit), (
        f"{label} host decode: {host_split!r} vs {host_nosplit!r}"
    )


def assert_forced_split_venv_parity(
    grid: list[Any] | list[list[Any]],
    result_split: Any,
    result_nosplit: Any,
    *,
    label: str,
) -> None:
    """Venv echo: always vs never may differ in nesting; leaf cells must match."""
    assert flatten_semantic_cells(result_split) == flatten_semantic_cells(result_nosplit), (
        f"{label}: {result_split!r} vs {result_nosplit!r}"
    )


def assert_venv_always_never_parity(
    grid: list[Any] | list[list[Any]],
    code: str,
    *,
    use_subprocess: bool = False,
    label: str = "",
) -> tuple[Any, Any]:
    """Run worker code with force=always vs force=never; leaf cells must match."""
    result_always = run_venv_roundtrip(grid, code, pack_force="always", use_subprocess=use_subprocess)
    result_never = run_venv_roundtrip(grid, code, pack_force="never", use_subprocess=use_subprocess)
    assert_forced_split_venv_parity(grid, result_always, result_never, label=label or "venv")
    return result_always, result_never


def assert_identity_after_echo(
    grid: list[Any] | list[list[Any]],
    final: Any,
    *,
    label: str,
) -> None:
    """Original grid vs venv echo result (handles single-cell scalar coercion)."""
    assert flatten_semantic_cells(grid) == flatten_semantic_cells(final), (
        f"{label}: grid {grid!r} vs final {final!r}"
    )


def forced_split_hard_cases() -> list[AbGridCase]:
    """Tiny grids forced through split_grid (force=always) — below BINARY_MIN_CELLS."""
    return [
        AbGridCase("forced_1cell_numeric_2d", [[42.0]], frozenset({"forced_split", "below_threshold"})),
        AbGridCase("forced_1cell_numeric_1d", [42.0], frozenset({"forced_split", "below_threshold"})),
        AbGridCase("forced_1cell_int", [[100]], frozenset({"forced_split", "below_threshold"})),
        AbGridCase("forced_1cell_mixed_zip", [["02138"]], frozenset({"forced_split", "mixed", "zip_code"})),
        AbGridCase("forced_1cell_bool", [[True]], frozenset({"forced_split", "bool"})),
        AbGridCase("forced_2x2_numeric", [[1.0, 2.0], [3.0, 4.0]], frozenset({"forced_split", "below_threshold"})),
        AbGridCase("forced_2x2_mixed", [[1.0, "a"], [2.0, "b"]], frozenset({"forced_split", "mixed", "below_threshold"})),
        AbGridCase(
            "forced_1x9_row",
            [[float(i + 1) for i in range(9)]],
            frozenset({"forced_split", "below_threshold", "flat"}),
            calc_shape=True,
        ),
        AbGridCase(
            "forced_9x1_col",
            [[float(i + 1)] for i in range(9)],
            frozenset({"forced_split", "below_threshold", "flat"}),
            calc_shape=True,
        ),
    ]


def all_codec_ab_cases() -> list[AbGridCase]:
    """Full corpus plus forced-split hard cases (unique ids)."""
    seen: set[str] = set()
    out: list[AbGridCase] = []
    for case in [*all_ab_grid_cases(), *forced_split_hard_cases()]:
        if case.id not in seen:
            seen.add(case.id)
            out.append(case)
    return out


def run_venv_roundtrip(
    grid: list[Any] | list[list[Any]],
    code: str,
    *,
    pack_force: ForceBinary = "auto",
    use_subprocess: bool = False,
    grid_b: list[Any] | list[list[Any]] | None = None,
) -> Any:
    """Pack on host, execute in venv sandbox (or warm subprocess), return host-side result."""
    if grid_b is not None:
        wire = host_pack_multi_data([grid, grid_b], force=pack_force)
    else:
        wire = host_pack_data(grid, force=pack_force)
    if use_subprocess:
        mgr = PythonWorkerManager.get(sys.executable, _WORKER_ENV)
        response = mgr.execute(code, data=wire)
        if response.get("status") != "ok":
            raise AssertionError(f"Worker error: {response.get('message')}")
        return response.get("result")
    response = _execute_request(code, wire)
    if response.get("status") != "ok":
        raise AssertionError(f"Sandbox error: {response.get('message')}")
    result = response.get("result")
    if result is None:
        return None
    if is_split_grid(result):
        return host_unpack_data(result, as_nested_list=True)
    return result


def normalize_for_oracle(value: Any) -> Any:
    """Normalize values for identity comparison after a full worker round-trip."""
    import numpy as np

    if isinstance(value, np.ndarray):
        value = value.tolist()
    if isinstance(value, (list, tuple)):
        return [normalize_for_oracle(v) for v in value]
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if value == float("inf") or value == float("-inf"):
            return value
        if value.is_integer():
            return int(value)
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    return value


def assert_semantically_equal(a: Any, b: Any, *, label: str = "") -> None:
    """Assert two results are semantically equivalent (ndarray vs list, NaN, scalars)."""
    import numpy as np

    a_norm = normalize_for_oracle(a)
    b_norm = normalize_for_oracle(b)

    if isinstance(a_norm, list) and isinstance(b_norm, list):
        assert len(a_norm) == len(b_norm), f"{label}: list length mismatch"
        for i, (x, y) in enumerate(zip(a_norm, b_norm)):
            assert_semantically_equal(x, y, label=f"{label}[{i}]")
        return

    if isinstance(a_norm, float) and isinstance(b_norm, float):
        if math.isnan(a_norm) and math.isnan(b_norm):
            return
        assert math.isclose(a_norm, b_norm, rel_tol=1e-9, abs_tol=1e-9), (
            f"{label}: float mismatch {a_norm} vs {b_norm}"
        )
        return

    if isinstance(a_norm, (int, float)) and isinstance(b_norm, (int, float)):
        assert math.isclose(float(a_norm), float(b_norm), rel_tol=1e-9, abs_tol=1e-9), (
            f"{label}: numeric mismatch {a_norm} vs {b_norm}"
        )
        return

    assert a_norm == b_norm, f"{label}: value mismatch {a_norm!r} vs {b_norm!r}"


def expect_child_list_not_ndarray(grid: list[Any] | list[list[Any]]) -> bool:
    """Mixed grids materialize as nested lists in child; numeric-only as ndarray."""
    if not grid:
        return False
    if isinstance(grid[0], (list, tuple)):
        rows = grid
    else:
        rows = [grid]
    for row in rows:
        for cell in row:
            if isinstance(cell, str):
                return True
    return False


def _grid_is_numeric_only(grid: list[Any] | list[list[Any]]) -> bool:
    if not grid:
        return False
    if isinstance(grid[0], (list, tuple)):
        return is_numeric_grid([list(row) for row in grid])
    return is_numeric_grid([grid])


def venv_transform_cases() -> list[VenvTransformCase]:
    """Subset of grids × transforms for worker integration tests."""
    cases: list[VenvTransformCase] = []
    
    # Single-range cases (existing)
    key_grids = [
        c for c in all_ab_grid_cases()
        if c.id in (
            "numeric_4x4",
            "mixed_zip",
            "mixed_label",
            "exactly_threshold",
            "flat_1d_mixed",
            "case_nan_holes_nansum",
            "case_mixed_cols_sum",
        )
    ]
    for grid_case in key_grids:
        grid = prepare_grid(grid_case)
        numeric_only = _grid_is_numeric_only(grid)
        for tname, code in VENV_TRANSFORMS.items():
            if tname == "sum" and not numeric_only:
                continue
            if tname == "double" and not numeric_only:
                continue
            if tname == "nansum" and "nan" not in grid_case.tags and "empty" not in grid_case.tags:
                continue
            if tname == "mixed_sum" and "mixed" not in grid_case.tags:
                continue
            cases.append(
                VenvTransformCase(
                    id=f"{grid_case.id}_{tname}",
                    grid=grid,
                    code=code,
                    expected=grid_case.expected if tname == "sum" and grid_case.expected is not None else None,
                    tags=grid_case.tags,
                )
            )

    # Multi-range cases
    multi_numeric = [[[1.0, 2.0], [3.0, 4.0]], [[5.0, 6.0], [7.0, 8.0]]]
    cases.append(
        VenvTransformCase(
            id="multi_numeric_sum_all",
            grid=multi_numeric[0],
            grid_b=multi_numeric[1],
            code="result = float(sum(np.sum(d) for d in data_list))",
            expected=36.0,
            tags=frozenset({"multi_range", "split_grid"}),
        )
    )
    cases.append(
        VenvTransformCase(
            id="multi_numeric_concatenate",
            grid=multi_numeric[0],
            grid_b=multi_numeric[1],
            code="result = np.concatenate(data_list).tolist()",
            expected=[[1.0, 2.0], [3.0, 4.0], [5.0, 6.0], [7.0, 8.0]],
            tags=frozenset({"multi_range", "split_grid"}),
        )
    )

    cases.append(
        VenvTransformCase(
            id="numeric_4x4_sum_subprocess",
            grid=NUMERIC_4X4,
            code=VENV_CODE_SUM,
            expected=264.0,
            tags=frozenset({"split_grid", "integration"}),
            use_subprocess=True,
        )
    )
    cases.append(
        VenvTransformCase(
            id="mixed_zip_echo_subprocess",
            grid=MIXED_WITH_ZIP,
            code=VENV_CODE_ECHO,
            tags=frozenset({"mixed", "integration"}),
            use_subprocess=True,
        )
    )
    return cases


def venv_expected_cases() -> list[VenvTransformCase]:
    """Cases with known expected values from serialization_cases."""
    out: list[VenvTransformCase] = []
    skip_ids = frozenset({"case_nan_holes_nansum"})  # nansum oracle differs from Calc SUM note in cases
    for case in all_ab_grid_cases():
        if case.expected is None or case.venv_code is None:
            continue
        if case.id in skip_ids:
            continue
        out.append(
            VenvTransformCase(
                id=f"expected_{case.id}",
                grid=prepare_grid(case),
                grid_b=prepare_grid(
                    AbGridCase(id=case.id, grid=case.grid_b, calc_shape=case.calc_shape, tags=case.tags)
                )
                if case.grid_b is not None
                else None,
                code=case.venv_code,
                expected=case.expected,
                tags=case.tags,
            )
        )
    return out


# --- Hypothesis strategies ---

# Diverse strings that Calc might contain (including edge cases like blank or numeric-looking)
CALC_STRINGS = st.one_of(
    st.sampled_from(["02138", "90210", "TRUE", "FALSCH", "0", "1", "1.5", "inf", "nan", "café", "日本語", "🎉", "long_string_" * 10]),
    st.text(min_size=1, max_size=20),
    st.text(alphabet=st.characters(whitelist_categories=("Zs", "Zl", "Zp")), min_size=1, max_size=5) # Blank strings
)


@strategies.composite
def grid_cell(draw) -> Any:
    # 0: None, 1: int, 2: float, 3: bool, 4: calc string, 5: numpy scalar
    kind = draw(st.integers(0, 5))
    if kind == 0:
        return None
    if kind == 1:
        # Safe integer range for double-precision float (Calc's internal representation)
        return draw(st.integers(-2**53 + 1, 2**53 - 1))
    if kind == 2:
        return draw(st.floats(allow_nan=True, allow_infinity=True, width=64))
    if kind == 3:
        return draw(st.booleans())
    if kind == 4:
        return draw(CALC_STRINGS)
    # NumPy scalars can happen when venv returns data
    import numpy as np
    nk = draw(st.integers(0, 2))
    if nk == 0: return np.float64(draw(st.floats()))
    if nk == 1:
        return np.int64(draw(st.integers(-2**53 + 1, 2**53 - 1)))
    return np.bool_(draw(st.booleans()))


# Hypothesis fuzz targets variety (cell types, 1D vs 2D), not size — boundary/large
# grids live in the named parametrized fixture corpus.
_HYPOTHESIS_MAX_CELLS = 10
_HYPOTHESIS_MAX_ROWS = 3
_HYPOTHESIS_MAX_COLS = 4

_NUMERIC_CELL = st.one_of(st.none(), st.integers(-2**53, 2**53), st.floats(allow_nan=True))


@strategies.composite
def rectangular_grid(
    draw,
    *,
    max_rows: int = _HYPOTHESIS_MAX_ROWS,
    max_cols: int = _HYPOTHESIS_MAX_COLS,
    max_cells: int = _HYPOTHESIS_MAX_CELLS,
) -> list[Any] | list[list[Any]]:
    ncells = draw(st.integers(1, max_cells))

    use_1d = draw(st.booleans())
    if use_1d:
        return [draw(grid_cell()) for _ in range(ncells)]

    # Bias toward 2×2 when small; variety over size (split_grid is forced on tiny grids).
    if ncells <= 4:
        limit_rows = min(2, max_rows)
        limit_cols = min(2, max_cols)
    else:
        limit_rows = max_rows
        limit_cols = max_cols

    nrows = draw(st.integers(1, min(ncells, limit_rows)))
    ncols = (ncells + nrows - 1) // nrows
    if ncols > limit_cols:
        ncols = limit_cols

    return [[draw(grid_cell()) for _ in range(ncols)] for _ in range(nrows)]


@strategies.composite
def numeric_rectangular_grid(draw) -> list[Any] | list[list[Any]]:
    # Numeric-only grids for np.sum parity; same small caps as rectangular_grid.
    use_1d = draw(st.booleans())
    if use_1d:
        n = draw(st.integers(1, _HYPOTHESIS_MAX_CELLS))
        return [draw(_NUMERIC_CELL) for _ in range(n)]

    ncells = draw(st.integers(1, _HYPOTHESIS_MAX_CELLS))
    if ncells <= 4:
        limit_rows = 2
        limit_cols = 2
    else:
        limit_rows = _HYPOTHESIS_MAX_ROWS
        limit_cols = _HYPOTHESIS_MAX_COLS

    nrows = draw(st.integers(1, min(ncells, limit_rows)))
    ncols = (ncells + nrows - 1) // nrows
    if ncols > limit_cols:
        ncols = limit_cols
    return [[draw(_NUMERIC_CELL) for _ in range(ncols)] for _ in range(nrows)]


def is_valid_grid_for_pack(grid: list[Any] | list[list[Any]]) -> bool:
    if not grid:
        return False
    if len(grid) == 1 and grid[0] is None:
        return False
    if isinstance(grid[0], (list, tuple)):
        ncols = len(grid[0])
        return all(len(row) == ncols for row in grid)
    return True


def _grid_has_string_cell(grid: list[Any] | list[list[Any]]) -> bool:
    if not grid:
        return False
    if isinstance(grid[0], (list, tuple)):
        return any(isinstance(cell, str) for row in grid for cell in row)
    return any(isinstance(cell, str) for cell in grid)


def _grid_has_blank_string(grid: list[Any] | list[list[Any]]) -> bool:
    def is_blank(cell: Any) -> bool:
        return isinstance(cell, str) and cell.strip() == ""

    if not grid:
        return False
    if isinstance(grid[0], (list, tuple)):
        return any(is_blank(cell) for row in grid for cell in row)
    return any(is_blank(cell) for cell in grid)


def _iter_flat_cells(grid: list[Any] | list[list[Any]]) -> list[Any]:
    if not grid:
        return []
    if isinstance(grid[0], (list, tuple)):
        return [cell for row in grid for cell in row]
    return list(grid)


def _cell_is_none_or_nan(cell: Any) -> bool:
    if cell is None:
        return True
    if isinstance(cell, float) and math.isnan(cell):
        return True
    import numpy as np

    if isinstance(cell, np.floating) and math.isnan(float(cell)):
        return True
    return False


def _grid_all_none_or_nan(grid: list[Any] | list[list[Any]]) -> bool:
    flat = _iter_flat_cells(grid)
    return bool(flat) and all(_cell_is_none_or_nan(c) for c in flat)


def hypothesis_grid_ok(grid: list[Any] | list[list[Any]]) -> bool:
    """Grids safe for codec + venv A/B fuzzing (skip json-list paths that choke on strings)."""
    if not is_valid_grid_for_pack(grid):
        return False
    if _grid_has_blank_string(grid):
        return False
    if _grid_all_none_or_nan(grid):
        return False
    if grid_cell_count(grid) < BINARY_MIN_CELLS and _grid_has_string_cell(grid):
        return False
    return True


@strategies.composite
def multi_range_grid(
    draw,
    *,
    min_ranges: int = 2,
    max_ranges: int = 5,
) -> list[list[Any] | list[list[Any]]]:
    """Generate a list of 1D/2D grids for multi-range serialization fuzzing."""
    num_ranges = draw(st.integers(min_ranges, max_ranges))
    return [draw(rectangular_grid()) for _ in range(num_ranges)]


# --- Multi-range (varargs) helpers ---

MULTI_RANGE_FIXTURES: list[tuple[list[list[Any] | list[list[Any]]], str]] = [
    ([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]], "multi_two_numeric_cols"),
    ([[[1.0, "a"], [2.0, "b"]], [[3.0, "c"]]], "multi_mixed_and_small"),
]


def multi_range_child_materialization(
    grids: list[list[Any] | list[list[Any]]],
    *,
    force: ForceBinary = "auto",
) -> Any:
    """Child-side materialization for a list of Calc ranges (multi_data envelope)."""
    return child_unpack_data(host_pack_multi_data(grids, force=force))


def run_multi_venv_echo(
    grids: list[list[Any] | list[list[Any]]],
    *,
    pack_force: ForceBinary = "auto",
    use_subprocess: bool = False,
) -> Any:
    """Echo ``data`` through venv when injected as a multi-range list."""
    code = "result = data"
    wire = host_pack_multi_data(grids, force=pack_force)
    if use_subprocess:
        mgr = PythonWorkerManager.get(sys.executable, _WORKER_ENV)
        response = mgr.execute(code, data=wire)
        if response.get("status") != "ok":
            raise AssertionError(f"Worker error: {response.get('message')}")
        return response.get("result")
    response = _execute_request(code, wire)
    if response.get("status") != "ok":
        raise AssertionError(f"Sandbox error: {response.get('message')}")
    result = response.get("result")
    if result is None:
        return None
    return host_unpack_data(result, as_nested_list=True)


__all__ = [
    "AbGridCase",
    "VenvTransformCase",
    "VENV_CODE_ECHO",
    "VENV_CODE_SUM",
    "VENV_TRANSFORMS",
    "all_ab_grid_cases",
    "prepare_grid",
    "grid_cell_count",
    "codec_child_materialization",
    "codec_host_decode",
    "flatten_semantic_cells",
    "assert_codec_split_vs_nosplit_parity",
    "assert_forced_split_venv_parity",
    "assert_venv_always_never_parity",
    "assert_identity_after_echo",
    "forced_split_hard_cases",
    "all_codec_ab_cases",
    "run_venv_roundtrip",
    "normalize_for_oracle",
    "assert_semantically_equal",
    "expect_child_list_not_ndarray",
    "venv_transform_cases",
    "venv_expected_cases",
    "rectangular_grid",
    "numeric_rectangular_grid",
    "is_valid_grid_for_pack",
    "hypothesis_grid_ok",
    "MULTI_RANGE_FIXTURES",
    "multi_range_child_materialization",
    "run_multi_venv_echo",
    "BINARY_MIN_CELLS",
    "multi_range_grid",
]

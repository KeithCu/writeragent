# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Calc =PY() error code mapping and value formatting in addin_values."""

from __future__ import annotations

import datetime
import logging
import math
import struct
from typing import Any

import pytest

from plugin.calc.python import addin_values
from plugin.calc.python.addin_values import to_calc_compatible
from plugin.scripting.venv.coerce import LO_ERROR_TOKENS

_VENV_GUIDANCE = "Error: Python venv not found. Open Settings → Python, set the venv path, then Test."
_TIMEOUT_GUIDANCE = "Error: Python timed out. Open Settings → Python to raise the timeout, or Test the venv."


def _nan_low16(val: float) -> int:
    raw = struct.unpack("Q", struct.pack("d", val))[0]
    return raw & 0xFFFF


def test_calc_error_tokens_agree_with_child() -> None:
    assert LO_ERROR_TOKENS == frozenset(addin_values._CALC_ERROR_TOKEN_CODES)
    assert len(addin_values._CALC_ERROR_TOKEN_CODES) == 8
    assert set(addin_values._CALC_ERROR_TOKEN_CODES.values()) == {519, 503, 532, 32767, 524, 525, 521}


@pytest.mark.parametrize(
    ("res", "expected"),
    [
        (
            {
                "status": "error",
                "code": "VENV_NOT_FOUND",
                "message": "No python executable found under configured venv: '/missing'",
            },
            _VENV_GUIDANCE,
        ),
        (
            {
                "status": "error",
                "code": "VENV_NOT_FOUND",
                "message": "Could not resolve a Python interpreter (sys.executable missing, ...)",
            },
            _VENV_GUIDANCE,
        ),
        (
            {
                "status": "error",
                "code": "VENV_TIMEOUT",
                "message": "Python worker failed: timed out after 30 seconds Shared Python process restarted (all workbooks).",
            },
            _TIMEOUT_GUIDANCE,
        ),
        (
            {
                "status": "error",
                "code": "EXECUTION_TIMEOUT",
                "message": "Code execution exceeded the maximum execution time of 30 seconds",
            },
            _TIMEOUT_GUIDANCE,
        ),
        (
            {
                "status": "error",
                "code": "EXECUTION_TIMEOUT",
                "message": "Insufficient time to spawn worker.",
            },
            _TIMEOUT_GUIDANCE,
        ),
        (
            {
                "status": "error",
                "code": "EXECUTION_TIMEOUT",
                "message": "python worker spawn handshake timed out",
            },
            _TIMEOUT_GUIDANCE,
        ),
        (
            {
                "status": "error",
                "code": "USER_STOPPED",
                "message": "Stopped by user.",
            },
            "Error: Stopped by user.",
        ),
        (
            {
                "status": "error",
                "code": "VENV_EXEC_ERROR",
                "message": "Code execution failed at line '1/0' due to: ZeroDivisionError: division by zero",
            },
            532,
        ),
        (
            {
                "status": "error",
                "code": "VENV_EXEC_ERROR",
                "message": "result must not be a socket timeout object",
            },
            519,
        ),
        (
            {
                "status": "error",
                "code": "WORKER_IPC_ERROR",
                "message": "Python worker failed: host RPC response timed out after 10 seconds ...",
            },
            519,
        ),
        (
            {
                "status": "error",
                "message": "Python worker failed: timed out after 10 seconds",
            },
            519,
        ),
        (
            {
                "status": "error",
                "code": "CANCELLED",
                "message": "any",
            },
            519,
        ),
    ],
)
def test_addin_result_for_worker_status_by_code(
    res: dict[str, Any],
    expected: str | int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="plugin.calc.python.addin_values"):
        out = addin_values.addin_result_for_worker_status(res)

    error_records = [
        r for r in caplog.records
        if r.name == "plugin.calc.python.addin_values" and r.levelno >= logging.ERROR
    ]
    if isinstance(expected, str):
        assert out == expected
        assert error_records == []
    else:
        assert isinstance(out, float)
        assert math.isnan(out)
        assert _nan_low16(out) == expected
        assert len(error_records) == 1


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (lambda: addin_values.format_addin_error_for_display(RuntimeError("")), "Error: RuntimeError"),
        (lambda: addin_values.format_addin_error_for_display(RuntimeError("x")), "Error: x"),
        (
            lambda: addin_values.addin_result_for_worker_status({"code": "USER_STOPPED", "message": "#SPILL!"}),
            "#SPILL!",
        ),
        (
            lambda: addin_values.addin_result_for_worker_status({"code": "USER_STOPPED", "message": ""}),
            "Error: Unknown error",
        ),
        (
            lambda: addin_values.addin_result_for_worker_status({"code": "USER_STOPPED", "message": "Error: x"}),
            "Error: x",
        ),
    ],
)
def test_error_cell_text_prefix_rules(call: Any, expected: str) -> None:
    assert call() == expected


def test_format_addin_error_for_display_keeps_host_wording() -> None:
    out = addin_values.format_addin_error_for_display(
        RuntimeError("marshal post timed out: AsyncCallback unavailable")
    )
    assert out == "Error: marshal post timed out: AsyncCallback unavailable"
    assert "Settings" not in out


@pytest.mark.parametrize(
    ("raw", "expected_calc", "expected_kind", "expected_cat", "expected_err", "expected_reg"),
    [
        (None, "", "e", None, None, None),
        ("", "", "e", None, None, None),
        ("  ", "", "e", None, None, None),
        (True, 1.0, "n", None, None, 1.0),
        (7, 7.0, "n", None, None, 7.0),
        (10**400, str(10**400), "s", None, None, str(10**400)),
        (123.45, 123.45, "n", None, None, 123.45),
        ("Hello World", "Hello World", "s", None, None, "Hello World"),
        ("#N/A", None, "err", None, 32767, 32767),
        ("#VALUE!", "", "err", None, 519, 519),
        (datetime.date(2026, 8, 8), 46242.0, "n", "date", None, 46242.0),
        (datetime.datetime(2026, 8, 8, 12), 46242.5, "n", "datetime", None, 46242.5),
        (
            datetime.datetime(2026, 8, 8, 12, tzinfo=datetime.timezone.utc),
            46242.5,
            "n",
            "datetime",
            None,
            46242.5,
        ),
        (datetime.time(12), 0.5, "n", "time", None, 0.5),
        (datetime.timedelta(hours=30), 1.25, "n", "duration", None, 1.25),
        ("2026-08-08", 46242.0, "n", "date", None, 46242.0),
        ("2026-08-08T12:00:00", 46242.5, "n", "datetime", None, 46242.5),
        ("2026-08-08 12:00:00", 46242.5, "n", "datetime", None, 46242.5),
        ("12:30:00", 45000.0 / 86400.0, "n", "time", None, 45000.0 / 86400.0),
        ("PT30H", 1.25, "n", "duration", None, 1.25),
        ("2026-02-30", "2026-02-30", "s", None, None, "2026-02-30"),
    ],
)
def test_coerce_spill_value_record(
    raw: Any,
    expected_calc: Any,
    expected_kind: str,
    expected_cat: str | None,
    expected_err: int | None,
    expected_reg: Any,
) -> None:
    rec = addin_values.coerce_spill_value(raw)
    if isinstance(expected_calc, float):
        assert rec.calc_value == pytest.approx(expected_calc, rel=1e-9, abs=1e-9)
        assert rec.registry_value == pytest.approx(expected_reg, rel=1e-9, abs=1e-9)
    else:
        assert rec.calc_value == expected_calc
        assert rec.registry_value == expected_reg
    assert rec.kind == expected_kind
    assert rec.temporal_category == expected_cat
    assert rec.error_code == expected_err


def test_to_stdlib_temporal_and_both_ladders() -> None:
    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")

    assert addin_values.to_stdlib_temporal(pd.NaT) is None
    assert addin_values.to_stdlib_temporal(pd.NA) is None
    assert addin_values.to_stdlib_temporal(np.datetime64("NaT")) is None
    assert addin_values.to_stdlib_temporal(np.timedelta64("NaT")) is None

    assert addin_values.to_stdlib_temporal(np.datetime64("2026-06-25")) == datetime.date(2026, 6, 25)
    assert addin_values.to_stdlib_temporal(np.datetime64("2026-06-25T00:00:00.500", "ms")) == datetime.datetime(
        2026, 6, 25, 0, 0, 0, 500000
    )
    ns_dt = np.datetime64("2026-06-25T00:00:00.500", "ns")
    assert addin_values.to_stdlib_temporal(ns_dt) == str(ns_dt)
    assert addin_values.to_stdlib_temporal(np.timedelta64(36, "h")) == datetime.timedelta(hours=36)
    assert addin_values.to_stdlib_temporal(np.timedelta64(500, "ns")) == "500 nanoseconds"

    ts = pd.Timestamp("2026-06-25 12:00:00")
    assert addin_values.to_stdlib_temporal(ts) is ts
    assert addin_values.to_stdlib_temporal(5) == 5

    assert addin_values.to_calc_compatible(np.datetime64("2026-06-25")) == "2026-06-25"
    assert addin_values.to_calc_compatible(np.timedelta64(36, "h")) == 1.5

    date_rec = addin_values.coerce_spill_value(np.datetime64("2026-06-25"))
    assert date_rec.kind == "n"
    assert date_rec.temporal_category == "date"

    nat_rec = addin_values.coerce_spill_value(pd.NaT)
    assert nat_rec.kind == "e"
    assert nat_rec.calc_value == ""

    ns_rec = addin_values.coerce_spill_value(ns_dt)
    assert ns_rec.kind == "s"
    assert ns_rec.calc_value == str(ns_dt)


def test_to_calc_compatible_none_becomes_empty_nan_becomes_error() -> None:
    """None (from text/mixed or explicit) becomes empty cell; NaN is returned raw (Calc shows cascading error)."""
    import math
    assert to_calc_compatible(None) == ""
    assert math.isnan(to_calc_compatible(float("nan")))



def test_to_calc_compatible_finite_float_unchanged() -> None:
    assert to_calc_compatible(3.5) == 3.5



def test_to_calc_compatible_bool_coercion() -> None:
    # Python bool maps to float 1.0/0.0 for Calc compatibility (#413).
    res_true = to_calc_compatible(True)
    assert res_true == 1.0
    assert isinstance(res_true, float)

    res_false = to_calc_compatible(False)
    assert res_false == 0.0
    assert isinstance(res_false, float)



def test_to_calc_compatible_nan_in_nested_matrix() -> None:
    """NaN slots in a matrix result stay as NaN (Calc error cells); only None becomes empty."""
    import math
    matrix = ((1.0, float("nan")), (3.0, 4.0))
    out = to_calc_compatible(matrix)
    assert out[0][0] == 1.0
    assert math.isnan(out[0][1])
    assert out[1] == (3.0, 4.0)



def test_to_calc_compatible_datetime_types() -> None:
    """Datetime, date, time, and timedelta are converted to ISO strings or fractional day floats."""
    import datetime

    dt = datetime.datetime(2026, 8, 13, 14, 30, 0)
    d = datetime.date(2026, 8, 13)
    t = datetime.time(14, 30, 0)
    td = datetime.timedelta(days=1, hours=12)

    assert to_calc_compatible(dt) == "2026-08-13T14:30:00"
    assert to_calc_compatible(d) == "2026-08-13"
    assert to_calc_compatible(t) == "14:30:00"
    assert to_calc_compatible(td) == 1.5



def test_to_calc_compatible_inf_and_decimal() -> None:
    """±inf pass through as floats; Decimal becomes float. Neither is a new wire type."""
    from decimal import Decimal

    assert to_calc_compatible(float("inf")) == float("inf")
    assert to_calc_compatible(float("-inf")) == float("-inf")
    assert to_calc_compatible(Decimal("1.25")) == 1.25



def test_to_calc_compatible_tz_aware_datetime_strips_offset() -> None:
    """Calc cannot parse +HH:MM / Z; wall time is emitted as naive ISO."""
    import datetime

    dt = datetime.datetime(2026, 8, 13, 14, 30, 0, tzinfo=datetime.timezone.utc)
    assert to_calc_compatible(dt) == "2026-08-13T14:30:00"



def test_to_calc_compatible_pandas_nat_timestamp_datetime64() -> None:
    """NaT → empty; Timestamp → naive ISO; datetime64 → ISO-like text. No pandas import in addin_values.py."""
    pd = pytest.importorskip("pandas")
    np = pytest.importorskip("numpy")

    assert to_calc_compatible(pd.NaT) == ""
    ts = pd.Timestamp("2026-08-13 14:30:00")
    assert to_calc_compatible(ts) == "2026-08-13T14:30:00"
    ts_tz = pd.Timestamp("2026-08-13 14:30:00", tz="UTC")
    assert to_calc_compatible(ts_tz) == "2026-08-13T14:30:00"
    assert "+" not in str(to_calc_compatible(ts_tz))
    dt64 = np.datetime64("2026-06-25")
    out = to_calc_compatible(dt64)
    assert out == "2026-06-25"
    assert out != 20629.0



def test_to_calc_compatible_jagged_2d_rectangularization() -> None:
    """Jagged 2D lists are padded to a rectangular 2D matrix with empty strings."""
    jagged = [[1, 2, 3], [4, 5], [6]]
    out = to_calc_compatible(jagged)
    assert out == (
        (1.0, 2.0, 3.0),
        (4.0, 5.0, ""),
        (6.0, "", ""),
    )



def test_to_calc_compatible_duck_typed_numeric() -> None:
    """Custom objects with __float__ are coerced to float."""
    class CustomNumber:
        def __init__(self, val: float):
            self.val = val

        def __float__(self) -> float:
            return self.val

    assert to_calc_compatible(CustomNumber(42.5)) == 42.5



def test_format_error_for_display_distinguishes_timeout_error() -> None:
    """Issue #402: host marshal TimeoutError must not format as user venv settings guidance."""
    exc = TimeoutError("Main-thread execution of _workbook_session_id_impl timed out after 30.0s")
    formatted = addin_values.format_addin_error_for_display(exc)
    assert "Main-thread execution timed out" in formatted
    assert "Settings" not in formatted
    assert "Test the venv" not in formatted



def test_ragged_results_handling() -> None:
    """Item 4: Ragged results in flatten_result_values and result_as_spill_grid."""
    # flatten_result_values:
    # [[1, 2], 3] -> [1, 2, 3] (no TypeError)
    assert addin_values.flatten_result_values([[1, 2], 3]) == [1, 2, 3]
    assert addin_values.flatten_result_values([1, [2, 3]]) == [1, 2, 3]
    assert addin_values.flatten_result_values([1, 2, 3]) == [1, 2, 3]
    assert addin_values.flatten_result_values(42) == [42]
    assert addin_values.flatten_result_values([]) == []

    # result_as_spill_grid:
    # [[1, 2], 3] -> [[1, 2], [3]]
    assert addin_values.result_as_spill_grid([[1, 2], 3]) == [[1, 2], [3]]
    # [[1, 2], "hello"] -> [[1, 2], ["hello"]] (does not split strings into chars)
    assert addin_values.result_as_spill_grid([[1, 2], "hello"]) == [[1, 2], ["hello"]]
    assert addin_values.result_as_spill_grid([1, 2, 3]) == [[1], [2], [3]]
    assert addin_values.result_as_spill_grid([]) == []



def test_overflow_int_becomes_decimal_text() -> None:
    """to_calc_compatible(10**400) == str(10**400) and the same through coerce_spill_value. to_calc_compatible(float('nan')) stays NaN."""
    huge = 10**400
    huge_str = str(huge)
    assert addin_values.to_calc_compatible(huge) == huge_str

    rec = addin_values.coerce_spill_value(huge)
    assert rec.calc_value == huge_str
    assert rec.kind == "s"
    assert rec.temporal_category is None

    nan_val = addin_values.to_calc_compatible(float("nan"))
    assert math.isnan(nan_val)



def test_calc_formula_error_double_and_tokens() -> None:
    tokens = [
        ("#VALUE!", 519),
        ("  #VALUE!  ", 519),
        ("#NUM!", 503),
        ("#DIV/0!", 532),
        ("#N/A", 32767),
        ("#N/A N/A", 32767),
        ("#REF!", 524),
        ("#NAME?", 525),
        ("#NULL!", 521),
    ]
    for token, expected_code in tokens:
        val = addin_values.py_addin_cell_value(token)
        assert isinstance(val, float)
        assert not math.isfinite(val)
        raw = struct.unpack("Q", struct.pack("d", val))[0]
        assert raw & 0xFFFF == expected_code
        assert raw == (0x7FF8000000000000 | expected_code)
        assert addin_values.calc_error_code_for_token(token) == expected_code

    # Direct calc_formula_error_double
    for _token, code in tokens:
        d = addin_values.calc_formula_error_double(code)
        raw = struct.unpack("Q", struct.pack("d", d))[0]
        assert raw & 0xFFFF == code
        assert raw == (0x7FF8000000000000 | code)

    # Strings that stay str
    for non_err in ["hello", "#value!", "#VALUE! extra", "#SPILL!", "Error: Code execution failed"]:
        val = addin_values.py_addin_cell_value(non_err)
        assert val == non_err
        assert isinstance(val, str)
        assert addin_values.calc_error_code_for_token(non_err) is None

    # None, nan, inf
    assert addin_values.py_addin_cell_value(None) == ""
    nan_val = addin_values.py_addin_cell_value(float("nan"))
    assert isinstance(nan_val, float) and math.isnan(nan_val)
    assert struct.unpack("Q", struct.pack("d", nan_val))[0] & 0xFFFF == 0
    inf_val = addin_values.py_addin_cell_value(float("inf"))
    assert inf_val == float("inf")
    ninf_val = addin_values.py_addin_cell_value(float("-inf"))
    assert ninf_val == float("-inf")

    # Tuple maps only first element when it has error token
    tup = addin_values.py_addin_cell_value(("#DIV/0!", 1.0, "ok"))
    assert isinstance(tup, tuple)
    first_raw = struct.unpack("Q", struct.pack("d", tup[0]))[0]
    assert first_raw & 0xFFFF == 532
    assert tup[1] == 1.0
    assert tup[2] == "ok"



def test_worker_exception_error_code() -> None:
    cases = [
        ("Code execution failed at line '1/0' due to: ZeroDivisionError: division by zero", 532),
        ("Code execution failed at line 'x' due to: OverflowError: math range error", 503),
        ("Code execution failed at line 'x' due to: FloatingPointError: underflow", 503),
        ("Code execution failed at line 'x' due to: NameError: name 'nope' is not defined", 525),
        ("Code execution failed at line 'x' due to: UnboundLocalError: local variable referenced before assignment", 525),
        ("Code execution failed at line 'bad' due to: InterpreterError: name 'bad' is not defined", 519),
        ("Code execution failed at line 'x' due to: ValueError: invalid literal", 519),
        ("Code execution failed at line 'x' due to: IndexError: list index out of range", 519),
        ("Code execution failed at line 'x' due to: NotImplementedError: bahttext", 519),
        ("Code execution failed at line 'x' due to: ValueError: connection timeout while reading", 519),
        ("Code execution failed at line 'x' due to: NameError: nope due to: ZeroDivisionError: div zero", 532),
        ("Unknown worker error", 519),
    ]
    for msg, expected_code in cases:
        assert addin_values.worker_exception_error_code(msg) == expected_code



def test_py_addin_cell_value_helper_error_dict(caplog: pytest.LogCaptureFixture) -> None:
    from plugin.calc.analysis_egress import format_analysis_for_calc
    from plugin.calc.quant_egress import format_quant_for_calc

    quick = {
        "status": "error",
        "code": "UNKNOWN_COLUMN",
        "message": "Unknown columns: Region",
        "helper": "quick_stats",
    }
    # Egress formatters keep the dict as text rows. to_calc_compatible still stringifies it.
    assert format_analysis_for_calc(quick) == [["Analysis error (UNKNOWN_COLUMN)"], ["Unknown columns: Region"]]
    assert format_quant_for_calc(quick) == [["Quant error (UNKNOWN_COLUMN)"], ["Unknown columns: Region"]]
    assert to_calc_compatible(quick) == str(quick)
    assert isinstance(to_calc_compatible(quick), str)

    mapped = [
        (quick, 519),
        ({"status": "error", "code": "INVALID_PARAM", "message": "bad"}, 519),
        (
            {
                "status": "error",
                "code": "INVALID_PARAM",
                "message": "bad",
                "helper": "describe_data",
                "details": {"col": "A"},
            },
            519,
        ),
        (
            {
                "status": "error",
                "code": "MISSING_PACKAGE",
                "message": "scipy is required for detect_outliers.",
                "helper": "detect_outliers",
            },
            519,
        ),
        ({"status": "error", "code": "#VALUE!", "message": "x"}, 519),
        ({"status": "error", "code": "#NUM!", "message": "x"}, 503),
        ({"status": "error", "code": "#DIV/0!", "message": "x"}, 532),
        ({"status": "error", "code": "  #DIV/0!  ", "message": "x"}, 532),
        ({"status": "error", "code": "#N/A", "message": "x"}, 32767),
        ({"status": "error", "code": "#N/A N/A", "message": "x"}, 32767),
        ({"status": "error", "code": "#REF!", "message": "x"}, 524),
        ({"status": "error", "code": "#NAME?", "message": "x"}, 525),
        ({"status": "error", "code": "#NULL!", "message": "x"}, 521),
        ({"status": "error", "code": "ZeroDivisionError", "message": "x"}, 532),
        ({"status": "error", "code": "OverflowError", "message": "x"}, 503),
        ({"status": "error", "code": "FloatingPointError", "message": "x"}, 503),
        ({"status": "error", "code": "NameError", "message": "x"}, 525),
        ({"status": "error", "code": "UnboundLocalError", "message": "x"}, 525),
        ({"status": "error", "code": "ValueError", "message": "x"}, 519),
        (
            {
                "status": "error",
                "code": "INVALID_PARAM",
                "message": "failed due to: ZeroDivisionError: div",
                "helper": "describe_data",
            },
            519,
        ),
    ]
    for err, bits in mapped:
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="plugin.calc.python.addin_values"):
            val = addin_values.py_addin_cell_value(err)
        assert isinstance(val, float)
        assert not math.isfinite(val)
        assert struct.unpack("Q", struct.pack("d", val))[0] & 0xFFFF == bits
        assert any(r.levelname == "ERROR" for r in caplog.records)

    stay_text = [
        {"a": 1, "b": 2},
        {"status": "ok", "helper": "describe_data", "metrics": {"n": 1}},
        {"status": "error", "message": "nope"},
        {"status": "error", "code": "UNKNOWN_COLUMN"},
        {"status": "error", "code": "", "message": "x"},
        {"status": "error", "code": "   ", "message": "x"},
        {"status": "error", "code": "UNKNOWN_COLUMN", "message": "Unknown columns: Region", "rows": [1]},
        {
            "status": "error",
            "code": "TEXT_ANALYTICS_ERROR",
            "helper": "topics",
            "message": "install scikit-learn for topics",
            "result": {"error": "MISSING_PACKAGE"},
        },
        {"status": "Error", "code": "X", "message": "Y"},
        {"status": "error", "code": 519, "message": "Y"},
        {"status": "error", "code": "X", "message": None},
        {"status": "error", "code": "X", "message": "Y", "helper": ""},
        {"status": "error", "code": "X", "message": "Y", "helper": "   "},
        {"status": "error", "code": "X", "message": "Y", "details": "nope"},
        {"status": "error", "code": "USER_STOPPED", "message": "Stopped by user.", "stdout": ""},
    ]
    for err in stay_text:
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="plugin.calc.python.addin_values"):
            val = addin_values.py_addin_cell_value(err)
        assert val == str(err)
        assert isinstance(val, str)
        assert caplog.text == ""


# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for shared helper-domain header/template glue."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from plugin.scripting.helper_domain import (
    build_helper_script_template,
    format_elapsed_time,
    parse_helper_script_header,
    parse_run_import_call_params,
    parse_run_import_call_spec,
    rps_insert_failed_outcome,
    script_header_needs_data_binding,
    supports_calc_or_writer_manual,
)
from plugin.scripting.python_runner import (
    prepend_run_import_document_bindings,
    script_uses_run_import,
)
import pytest


def test_parse_valid_header():
    code = '# writeragent:units helper=convert_quantity params={"value":"10"}\nresult = 1\n'
    meta = parse_helper_script_header(code, tag="units", helper_names={"convert_quantity"})
    assert meta is not None
    assert meta.helper == "convert_quantity"
    assert meta.params == {"value": "10"}


@pytest.mark.parametrize(
    "value, tag, value_2",
    [
        pytest.param('# writeragent:analysis helper=describe_data params={}\n', "units", "convert_quantity", id="test_parse_missing_tag"),
        pytest.param("# writeragent:analysis helper=not_real params={}\n", "analysis", "describe_data", id="test_parse_unknown_helper"),
    ],
)
def test_parse_missing_tag(value, tag, value_2):
    code = value
    assert parse_helper_script_header(code, tag=tag, helper_names={value_2}) is None

def test_parse_bad_json_empty():
    code = "# writeragent:units helper=convert_quantity params={not-json}\n"
    meta = parse_helper_script_header(
        code,
        tag="units",
        helper_names={"convert_quantity"},
        on_bad_json="empty",
    )
    assert meta is not None
    assert meta.params == {}


def test_parse_bad_json_none():
    code = "# writeragent:forecast helper=forecast_time_series params={not-json}\n"
    meta = parse_helper_script_header(
        code,
        tag="forecast",
        helper_names=None,
        require_prefix=False,
        on_bad_json="none",
    )
    assert meta is None


def test_build_run_import_template_has_header_and_import():
    body = build_helper_script_template(
        tag="units",
        helper="convert_quantity",
        params={"value": 10, "from": "m/s", "to": "km/h"},
        description="Convert",
        style="run_import",
        import_module="writeragent.scripting.units",
        run_name="run_units",
        data_expr="None",
        positional_args=("value", "from", "to"),
        extra_comment_lines=("# Edit the run call below, then Run.",),
    )
    assert not body.startswith("# writeragent:")
    assert body.startswith("# Convert")
    assert "convert_quantity(10, 'm/s', 'km/h')" in body
    assert "from writeragent.scripting.units import convert_quantity" in body


def test_build_run_import_template_with_data():
    body = build_helper_script_template(
        tag="units",
        helper="convert_quantity",
        params={"value": "data", "from": "m/s", "to": "km/h"},
        description="Convert",
        style="run_import",
        import_module="writeragent.scripting.units",
        run_name="run_units",
        data_expr="data",
        positional_args=("value", "from", "to"),
    )
    assert "convert_quantity(data, 'm/s', 'km/h')" in body


def test_parse_run_import_call_params_reads_body():
    code = (
        'result = run_units({"helper": "convert_quantity", "params": {"value":"20","to_unit":"mm/h"}}, None, {})\n'
    )
    params = parse_run_import_call_params(code, run_name="run_units")
    assert params == {"value": "20", "to_unit": "mm/h"}


def test_parse_run_import_call_spec_reads_helper_and_params():
    code = (
        'result = run_text_analytics({"helper": "entities", "params": {"lang": "de"}}, text, document_context)\n'
    )
    spec = parse_run_import_call_spec(code, run_name="run_text_analytics")
    assert spec == {"helper": "entities", "params": {"lang": "de"}}


def test_print_is_not_a_run_import_or_helper_spec():
    code = 'print("hi")\n'
    assert script_uses_run_import(code, run_name="run_text_analytics") is False
    assert parse_run_import_call_spec(code, run_name="run_text_analytics") is None


def test_writeragent_import_call_is_a_helper_spec():
    code = (
        "from writeragent.scripting.units import convert_quantity\n"
        'result = convert_quantity(10, "m", "km")\n'
    )
    spec = parse_run_import_call_spec(code, run_name="run_units")
    assert spec is not None
    assert spec["helper"] == "convert_quantity"
    assert spec["params"] == {"value": 10, "from": "m", "to": "km"}
    assert script_uses_run_import(code, run_name="run_text_analytics") is False


def test_direct_call_units_positional_prefix_and_keywords():
    code1 = (
        "from writeragent.scripting.units import convert_quantity\n"
        'convert_quantity(data, from_unit="m/s", to_unit="km/h")\n'
    )
    assert parse_run_import_call_spec(code1, run_name="run_units") == {
        "helper": "convert_quantity",
        "params": {"value": "data", "from_unit": "m/s", "to_unit": "km/h"},
    }

    code2 = (
        "from writeragent.scripting.units import convert_quantity\n"
        'convert_quantity(1, "m", "km", value=5)\n'
    )
    assert parse_run_import_call_spec(code2, run_name="run_units") == {
        "helper": "convert_quantity",
        "params": {"value": 5, "from": "m", "to": "km"},
    }

    code3 = (
        "from writeragent.scripting.units import convert_quantity\n"
        'convert_quantity(data, "m")\n'
    )
    assert parse_run_import_call_spec(code3, run_name="run_units") == {
        "helper": "convert_quantity",
        "params": {"value": "data", "from": "m"},
    }

    code4 = (
        "from writeragent.scripting.units import format_quantity\n"
        'format_quantity(50, "m", ".2f")\n'
    )
    assert parse_run_import_call_spec(code4, run_name="run_units") == {
        "helper": "format_quantity",
        "params": {"magnitude": 50, "units": "m", "format_spec": ".2f"},
    }

    code5 = (
        "from writeragent.scripting.units import format_quantity\n"
        "format_quantity(50)\n"
    )
    spec5 = parse_run_import_call_spec(code5, run_name="run_units")
    assert spec5 is not None
    assert spec5["params"] == {"magnitude": 50}
    assert "quantity" not in spec5["params"]

    code6 = (
        "from writeragent.scripting.units import parse_quantity\n"
        'parse_quantity("10 m/s")\n'
    )
    assert parse_run_import_call_spec(code6, run_name="run_units") == {
        "helper": "parse_quantity",
        "params": {"quantity": "10 m/s"},
    }

    code7 = (
        "from writeragent.scripting.units import check_dimensionality\n"
        'check_dimensionality("10 m", "10 ft")\n'
    )
    assert parse_run_import_call_spec(code7, run_name="run_units") == {
        "helper": "check_dimensionality",
        "params": {"quantity_a": "10 m", "quantity_b": "10 ft"},
    }

    code8 = (
        "from writeragent.scripting.units import check_dimensionality\n"
        'check_dimensionality("10 m")\n'
    )
    assert parse_run_import_call_spec(code8, run_name="run_units") == {
        "helper": "check_dimensionality",
        "params": {"quantity_a": "10 m"},
    }

    code9 = (
        "from writeragent.scripting.analysis import describe_data\n"
        "describe_data(data)\n"
    )
    assert parse_run_import_call_spec(code9, run_name="run_analysis") == {
        "helper": "describe_data",
        "params": {},
    }


def test_parse_run_import_call_null_byte_returns_none():
    assert parse_run_import_call_spec("\x00", run_name="\x00") is None
    assert parse_run_import_call_params("\x00", run_name="\x00") is None


def test_build_header_only_template():
    body = build_helper_script_template(
        tag="forecast",
        helper="forecast_time_series",
        params={"periods": 12},
        description="Forward",
        style="header_only",
        compact_json=False,
        extra_comment_lines=("# Edit the JSON params above if needed. No other code runs.",),
    )
    assert body.startswith("# writeragent:forecast helper=forecast_time_series")
    assert "from writeragent" not in body
    assert "Forward" in body


def test_format_elapsed_time_buckets():
    assert format_elapsed_time(0.0005) == "<1 ms"
    assert format_elapsed_time(0.05).endswith("ms")
    assert format_elapsed_time(2.5) == "2.50s"
    assert "m" in format_elapsed_time(65)


def test_prepend_run_import_document_bindings_uses_generic_comment():
    out = prepend_run_import_document_bindings("result = 1\n", bindings={"text": "hi"})
    assert out.startswith("# Document inputs injected below")
    assert "WriterAgent" not in out
    assert 'text = "hi"' in out
    assert out.endswith("result = 1\n")


def test_parse_run_import_call_params_nested_unary():
    code = (
        'result = run_units({"helper": "h", "params": {"n": -5, "items": [-1, 2], '
        '"pos": +3, "dbl": -(-4)}}, None, {})\n'
    )
    params = parse_run_import_call_params(code, run_name="run_units")
    assert params == {"n": -5, "items": [-1, 2], "pos": 3, "dbl": 4}


def test_rps_insert_failed_outcome_empty_str_uses_repr():
    class Blank(Exception):
        def __str__(self) -> str:
            return "  "

    out = rps_insert_failed_outcome(Blank(), t0=0.0)
    assert out["ok"] is False
    assert "Blank()" in out["message"]
    assert "result:  (" not in out["message"]


def test_rps_insert_failed_outcome_logs_type_str_repr(caplog):
    """RPS insert-fail path must log str/repr so Arch debug shows UNO errors."""
    err = RuntimeError("insertDocumentFromURL")
    with caplog.at_level(logging.ERROR, logger="writeragent.scripting"):
        out = rps_insert_failed_outcome(err, t0=0.0)
    assert out["ok"] is False
    assert "insertDocumentFromURL" in out["message"]
    assert any("rps_insert_failed_outcome" in r.message for r in caplog.records)
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in joined
    assert "insertDocumentFromURL" in joined


def test_parse_run_import_call_accepts_long_script():
    from plugin.framework.deal_shim import DEAL_MAX_SOURCE
    from plugin.scripting.helper_domain import parse_run_import_call_spec

    code = "x = 1\n" * (DEAL_MAX_SOURCE // 4)
    assert parse_run_import_call_spec(code, run_name="run") is None


def test_script_uses_run_import_in_comment_not_matched():
    # What was wrong: Substring search for run_name matched comments like "# run_vision(doc)",
    # wrongly treating custom Python scripts as helper-domain scripts.
    # Why this change: AST parsing ensures run_name is an actual function call, not comment text.
    code = "# run_vision(data)\nresult = 42\n"
    assert script_uses_run_import(code, run_name="run_vision") is False


def test_prepend_run_import_document_bindings_after_shebang_and_future():
    # What was wrong: Injected bindings prepended at index 0 broke Python shebangs,
    # encoding declarations, and __future__ imports (which must precede other code).
    # Why this change: _find_binding_insertion_line inserts after shebang, encoding, and __future__.
    code = (
        "#!/usr/bin/env python3\n"
        "# -*- coding: utf-8 -*-\n"
        "from __future__ import annotations\n"
        "result = 1\n"
    )
    res = prepend_run_import_document_bindings(code, bindings={"val": None, "active": True})
    lines = res.splitlines()
    assert lines[0] == "#!/usr/bin/env python3"
    assert lines[1] == "# -*- coding: utf-8 -*-"
    assert lines[2] == "from __future__ import annotations"
    assert lines[3].startswith("# Document inputs injected below")
    assert "val = None" in res
    assert "active = True" in res


def test_prepend_run_import_document_bindings_docstring_order():
    code1 = '"""Module doc."""\nresult = 1\n'
    res1 = prepend_run_import_document_bindings(code1, bindings={"text": "hi"})
    lines1 = res1.splitlines()
    assert lines1[0] == '"""Module doc."""'
    assert lines1[1].startswith("# Document inputs injected below")
    assert lines1[-1] == "result = 1"

    code2 = '"""Line one.\n\nLine two.\n"""\nresult = 1\n'
    res2 = prepend_run_import_document_bindings(code2, bindings={"text": "hi"})
    lines2 = res2.splitlines()
    assert lines2[3] == '"""'
    assert lines2[4].startswith("# Document inputs injected below")
    assert lines2[-1] == "result = 1"

    code3 = '#!/usr/bin/env python3\n"""doc"""\nx = 1\n'
    res3 = prepend_run_import_document_bindings(code3, bindings={"text": "hi"})
    lines3 = res3.splitlines()
    assert lines3[0] == "#!/usr/bin/env python3"
    assert lines3[1] == '"""doc"""'
    assert lines3[2].startswith("# Document inputs injected below")
    assert lines3[-1] == "x = 1"

    code4 = '"""doc"""\nfrom __future__ import annotations\nx = 1\n'
    res4 = prepend_run_import_document_bindings(code4, bindings={"text": "hi"})
    lines4 = res4.splitlines()
    assert lines4[0] == '"""doc"""'
    assert lines4[1] == "from __future__ import annotations"
    assert lines4[2].startswith("# Document inputs injected below")
    assert lines4[-1] == "x = 1"


def test_build_helper_script_template_bare_data_only_for_injected_data():
    # What was wrong: val == "data" in template params caused literal "data" strings
    # for non-data parameters (like column="data") to be unquoted as bare variable references.
    # Why this change: Only unquote when the parameter key is a known data/value/quantity arg.
    template = build_helper_script_template(
        tag="analysis",
        helper="describe_data",
        params={"column": "data", "data": "data"},
        description="Describe",
        style="run_import",
        import_module="writeragent.scripting.analysis",
        run_name="run_analysis",
        data_expr="data",
    )
    assert "column='data'" in template
    assert "data=data" in template


def test_format_elapsed_time_boundary_rounding():
    # What was wrong: 59.999s formatted as 60.00s instead of 1m 0s, and sub-second
    # times like 0.999s could round up to seconds prematurely.
    # Why this change: Round to 2 decimal places before testing >= 60.0, and keep >= 1.0 for seconds.
    assert format_elapsed_time(59.999) == "1m 0s"
    assert format_elapsed_time(59.994) == "59.99s"
    assert format_elapsed_time(0.999) == "999 ms"


def test_run_trusted_calc_data_helper_sheet_disposal_propagates():
    from plugin.scripting.helper_domain import run_trusted_calc_data_helper
    from plugin.framework.errors import DocumentDisposedError

    class DisposedException(Exception):
        pass

    client_mock = MagicMock()
    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
        patch("plugin.calc.calc_addin_data._resolve_python_data", return_value=([[1]], None)),
        patch("plugin.calc.bridge.CalcBridge") as mock_bridge,
    ):
        mock_bridge.return_value.get_active_sheet.side_effect = DisposedException("disposed")
        with pytest.raises(DocumentDisposedError):
            run_trusted_calc_data_helper(
                MagicMock(),
                MagicMock(),
                helper="forecast_time_series",
                helper_names=frozenset({"forecast_time_series"}),
                error_code="FORECAST_ERROR",
                empty_data_message="No data to forecast",
                data=[[1]],
                data_range=None,
                headers=True,
                params=None,
                task_hint=None,
                client_run=client_mock,
            )
    client_mock.assert_not_called()


def test_run_trusted_calc_data_helper_sheet_name_failure_is_best_effort():
    from plugin.scripting.helper_domain import run_trusted_calc_data_helper

    class RuntimeException(Exception):
        pass

    client_mock = MagicMock(return_value={"status": "ok"})
    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
        patch("plugin.calc.calc_addin_data._resolve_python_data", return_value=([[1]], None)),
        patch("plugin.calc.bridge.CalcBridge") as mock_bridge,
    ):
        mock_bridge.return_value.get_active_sheet.side_effect = RuntimeError("no sheet")
        res1 = run_trusted_calc_data_helper(
            MagicMock(),
            MagicMock(),
            helper="forecast_time_series",
            helper_names=frozenset({"forecast_time_series"}),
            error_code="FORECAST_ERROR",
            empty_data_message="No data to forecast",
            data=[[1]],
            data_range=None,
            headers=True,
            params=None,
            task_hint=None,
            client_run=client_mock,
        )
        assert res1 == {"status": "ok"}
        client_mock.assert_called_once()
        _, kwargs1 = client_mock.call_args
        assert kwargs1.get("context") is None

        client_mock.reset_mock()
        mock_bridge.return_value.get_active_sheet.side_effect = RuntimeException("runtime err")
        res2 = run_trusted_calc_data_helper(
            MagicMock(),
            MagicMock(),
            helper="forecast_time_series",
            helper_names=frozenset({"forecast_time_series"}),
            error_code="FORECAST_ERROR",
            empty_data_message="No data to forecast",
            data=[[1]],
            data_range=None,
            headers=True,
            params=None,
            task_hint=None,
            client_run=client_mock,
        )
        assert res2 == {"status": "ok"}
        client_mock.assert_called_once()
        _, kwargs2 = client_mock.call_args
        assert kwargs2.get("context") is None


def test_supports_calc_or_writer_manual():
    assert supports_calc_or_writer_manual(None) is False

    doc_writer = MagicMock()
    doc_writer.supportsService.side_effect = lambda s: s == "com.sun.star.text.TextDocument"
    assert supports_calc_or_writer_manual(doc_writer) is True

    doc_calc = MagicMock()
    doc_calc.supportsService.side_effect = lambda s: s == "com.sun.star.sheet.SpreadsheetDocument"
    assert supports_calc_or_writer_manual(doc_calc) is True

    doc_other = MagicMock()
    doc_other.supportsService.return_value = False
    assert supports_calc_or_writer_manual(doc_other) is False


def test_script_header_needs_data_binding_on_calc_domains():

    calc_doc = object()
    with patch("plugin.doc.doc_type.is_calc", return_value=True):
        assert script_header_needs_data_binding(
            "from writeragent.scripting.analysis import run_analysis\nresult = run_analysis(...)\n",
            doc=calc_doc,
        ) is True
        assert script_header_needs_data_binding(
            'from writeragent.scripting.forecast import run_forecast\nresult = run_forecast({"helper": "forecast_time_series", "params": {}}, data, {})\n',
            doc=calc_doc,
        ) is True
        assert script_header_needs_data_binding("# writeragent:text helper=full params={}\n", doc=calc_doc) is False
        assert script_header_needs_data_binding('print("hi")\n', doc=calc_doc) is False


def test_script_header_needs_data_binding_for_direct_helper_templates():
    from plugin.scripting.analysis import get_analysis_script_templates
    from plugin.scripting.duckdb_sql import get_sql_script_templates

    calc_doc = object()
    with patch("plugin.doc.doc_type.is_calc", return_value=True):
        assert script_header_needs_data_binding(
            get_analysis_script_templates()["describe_data"],
            doc=calc_doc,
        ) is True
        assert script_header_needs_data_binding(
            get_sql_script_templates()["query_sheet_sql"],
            doc=calc_doc,
        ) is True


def test_script_header_needs_data_binding_ignores_comments_and_non_calc():

    calc_doc = object()
    non_calc_doc = object()

    def is_calc_mock(doc: object) -> bool:
        return doc is calc_doc

    viz_code = "from writeragent.scripting.viz import quick_plot\nresult = quick_plot()\n"

    with patch("plugin.doc.doc_type.is_calc", side_effect=is_calc_mock):
        assert script_header_needs_data_binding("# run_analysis(foo)\n", doc=calc_doc) is False
        assert script_header_needs_data_binding('x = "run_analysis("\n', doc=calc_doc) is False
        assert script_header_needs_data_binding("run_analysis()\n", doc=calc_doc) is True
        assert (
            script_header_needs_data_binding(
                "result = writeragent.scripting.analysis.run_analysis()\n",
                doc=calc_doc,
            )
            is True
        )
        assert script_header_needs_data_binding("query_folder_sql()\n", doc=calc_doc) is True
        assert (
            script_header_needs_data_binding(
                "import writeragent.scripting.analysis as analysis\nresult = analysis.describe_data()\n",
                doc=calc_doc,
            )
            is True
        )
        assert (
            script_header_needs_data_binding(
                "import writeragent.scripting.analysis\nresult = writeragent.scripting.analysis.describe_data()\n",
                doc=calc_doc,
            )
            is False
        )
        assert script_header_needs_data_binding("run_analysis(\n", doc=calc_doc) is True
        assert script_header_needs_data_binding("run_viz()\n", doc=non_calc_doc) is False
        assert script_header_needs_data_binding(viz_code, doc=non_calc_doc) is False
        assert script_header_needs_data_binding(viz_code, doc=calc_doc) is True




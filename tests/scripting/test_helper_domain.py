# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for shared helper-domain header/template glue."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from plugin.scripting.helper_domain import (
    ScriptFacts,
    build_helper_script_template,
    format_elapsed_time,
    rps_insert_failed_outcome,
    supports_calc_or_writer_manual,
)
import pytest


def test_parse_valid_header():
    code = '# writeragent:units helper=convert_quantity params={"value":"10"}\nresult = 1\n'
    meta = ScriptFacts.from_source(code).header(tag="units", helper_names={"convert_quantity"})
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
    assert ScriptFacts.from_source(code).header(tag=tag, helper_names={value_2}) is None

def test_parse_bad_json_empty():
    code = "# writeragent:units helper=convert_quantity params={not-json}\n"
    meta = ScriptFacts.from_source(code).header(
        tag="units",
        helper_names={"convert_quantity"},
    )
    assert meta is not None
    assert meta.params == {}


def test_build_run_import_template_has_header_and_import():
    body = build_helper_script_template(
        helper="convert_quantity",
        params={"value": 10, "from": "m/s", "to": "km/h"},
        description="Convert",
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
        helper="convert_quantity",
        params={"value": "data", "from": "m/s", "to": "km/h"},
        description="Convert",
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
    spec = ScriptFacts.from_source(code).first_run_spec("run_units")
    assert spec is not None
    assert spec["params"] == {"value": "20", "to_unit": "mm/h"}


def test_parse_run_import_call_spec_reads_helper_and_params():
    code = (
        'result = run_text_analytics({"helper": "entities", "params": {"lang": "de"}}, text, document_context)\n'
    )
    spec = ScriptFacts.from_source(code).first_run_spec("run_text_analytics")
    assert spec == {"helper": "entities", "params": {"lang": "de"}}


def test_print_is_not_a_run_import_or_helper_spec():
    code = 'print("hi")\n'
    assert ScriptFacts.from_source(code).calls("run_text_analytics") is False
    assert ScriptFacts.from_source(code).first_run_spec("run_text_analytics") is None


def test_writeragent_import_call_is_a_helper_spec():
    code = (
        "from writeragent.scripting.units import convert_quantity\n"
        'result = convert_quantity(10, "m", "km")\n'
    )
    spec = ScriptFacts.from_source(code).first_run_spec("run_units")
    assert spec is not None
    assert spec["helper"] == "convert_quantity"
    assert spec["params"] == {"value": 10, "from": "m", "to": "km"}
    assert ScriptFacts.from_source(code).calls("run_text_analytics") is False


def test_direct_call_units_positional_prefix_and_keywords():
    code1 = (
        "from writeragent.scripting.units import convert_quantity\n"
        'convert_quantity(data, from_unit="m/s", to_unit="km/h")\n'
    )
    assert ScriptFacts.from_source(code1).first_run_spec("run_units") == {
        "helper": "convert_quantity",
        "params": {"value": "data", "from_unit": "m/s", "to_unit": "km/h"},
    }

    code2 = (
        "from writeragent.scripting.units import convert_quantity\n"
        'convert_quantity(1, "m", "km", value=5)\n'
    )
    assert ScriptFacts.from_source(code2).first_run_spec("run_units") == {
        "helper": "convert_quantity",
        "params": {"value": 5, "from": "m", "to": "km"},
    }

    code3 = (
        "from writeragent.scripting.units import convert_quantity\n"
        'convert_quantity(data, "m")\n'
    )
    assert ScriptFacts.from_source(code3).first_run_spec("run_units") == {
        "helper": "convert_quantity",
        "params": {"value": "data", "from": "m"},
    }

    code4 = (
        "from writeragent.scripting.units import format_quantity\n"
        'format_quantity(50, "m", ".2f")\n'
    )
    assert ScriptFacts.from_source(code4).first_run_spec("run_units") == {
        "helper": "format_quantity",
        "params": {"magnitude": 50, "units": "m", "format_spec": ".2f"},
    }

    code5 = (
        "from writeragent.scripting.units import format_quantity\n"
        "format_quantity(50)\n"
    )
    spec5 = ScriptFacts.from_source(code5).first_run_spec("run_units")
    assert spec5 is not None
    assert spec5["params"] == {"magnitude": 50}
    assert "quantity" not in spec5["params"]

    code6 = (
        "from writeragent.scripting.units import parse_quantity\n"
        'parse_quantity("10 m/s")\n'
    )
    assert ScriptFacts.from_source(code6).first_run_spec("run_units") == {
        "helper": "parse_quantity",
        "params": {"quantity": "10 m/s"},
    }

    code7 = (
        "from writeragent.scripting.units import check_dimensionality\n"
        'check_dimensionality("10 m", "10 ft")\n'
    )
    assert ScriptFacts.from_source(code7).first_run_spec("run_units") == {
        "helper": "check_dimensionality",
        "params": {"quantity_a": "10 m", "quantity_b": "10 ft"},
    }

    code8 = (
        "from writeragent.scripting.units import check_dimensionality\n"
        'check_dimensionality("10 m")\n'
    )
    assert ScriptFacts.from_source(code8).first_run_spec("run_units") == {
        "helper": "check_dimensionality",
        "params": {"quantity_a": "10 m"},
    }

    code9 = (
        "from writeragent.scripting.analysis import describe_data\n"
        "describe_data(data)\n"
    )
    assert ScriptFacts.from_source(code9).first_run_spec("run_analysis") == {
        "helper": "describe_data",
        "params": {},
    }


def test_parse_run_import_call_null_byte_returns_none():
    assert ScriptFacts.from_source("\x00").first_run_spec("\x00") is None


def test_format_elapsed_time_buckets():
    assert format_elapsed_time(0.0005) == "<1 ms"
    assert format_elapsed_time(0.05).endswith("ms")
    assert format_elapsed_time(2.5) == "2.50s"
    assert "m" in format_elapsed_time(65)


def test_parse_run_import_call_params_nested_unary():
    code = (
        'result = run_units({"helper": "h", "params": {"n": -5, "items": [-1, 2], '
        '"pos": +3, "dbl": -(-4)}}, None, {})\n'
    )
    spec = ScriptFacts.from_source(code).first_run_spec("run_units")
    assert spec is not None
    assert spec["params"] == {"n": -5, "items": [-1, 2], "pos": 3, "dbl": 4}


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

    code = "x = 1\n" * (DEAL_MAX_SOURCE // 4)
    assert ScriptFacts.from_source(code).first_run_spec("run") is None


def test_script_uses_run_import_in_comment_not_matched():
    code = "# run_vision(data)\nresult = 42\n"
    assert ScriptFacts.from_source(code).calls("run_vision") is False


def test_build_helper_script_template_bare_data_only_for_injected_data():
    template = build_helper_script_template(
        helper="describe_data",
        params={"column": "data", "data": "data"},
        description="Describe",
        import_module="writeragent.scripting.analysis",
        run_name="run_analysis",
        data_expr="data",
    )
    assert "column='data'" in template
    assert "data=data" in template


def test_direct_call_unresolved_name_is_omitted():
    code = (
        "from writeragent.scripting.units import convert_quantity\n"
        "convert_quantity(user_var)\n"
    )
    assert ScriptFacts.from_source(code).first_run_spec("run_units") == {
        "helper": "convert_quantity",
        "params": {},
    }


def test_unresolved_output_style_is_omitted():
    code = (
        "from writeragent.scripting.units import convert_quantity\n"
        "convert_quantity(1, 'm', 'km', output_style=style_var)\n"
    )
    spec = ScriptFacts.from_source(code).first_run_spec("run_units")
    assert spec is not None
    assert spec["params"] == {
        "value": 1,
        "from": "m",
        "to": "km",
    }


def test_literal_none_is_kept():
    code = (
        "from writeragent.scripting.units import convert_quantity\n"
        "convert_quantity(None)\n"
    )
    assert ScriptFacts.from_source(code).first_run_spec("run_units") == {
        "helper": "convert_quantity",
        "params": {"value": None},
    }


def test_unresolved_list_and_dict_value_are_omitted():
    code = (
        'result = run_units({"helper": "convert_quantity", "params": {"value": user_var, "to": "km", "items": [1, user_var]}}, None, {})\n'
    )
    spec = ScriptFacts.from_source(code).first_run_spec("run_units")
    assert spec is not None
    assert spec["params"] == {"to": "km"}


def test_format_elapsed_time_boundary_rounding():
    # format_elapsed_time rounds to two decimals before choosing minutes vs seconds.
    # Values like 59.999s must show as 1m 0s, and sub-second times stay in milliseconds.
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
        patch("plugin.framework.queue_executor.main.is_current", return_value=True),
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
        patch("plugin.framework.queue_executor.main.is_current", return_value=True),
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
    with patch("plugin.framework.doc_type.is_calc", return_value=True):
        assert ScriptFacts.from_source(
            "from writeragent.scripting.analysis import run_analysis\nresult = run_analysis(...)\n",
        ).needs_data_binding(doc=calc_doc) is True
        assert ScriptFacts.from_source(
            'from writeragent.scripting.forecast import run_forecast\nresult = run_forecast({"helper": "forecast_time_series", "params": {}}, data, {})\n',
        ).needs_data_binding(doc=calc_doc) is True
        assert ScriptFacts.from_source("# writeragent:text helper=full params={}\n").needs_data_binding(doc=calc_doc) is False
        assert ScriptFacts.from_source('print("hi")\n').needs_data_binding(doc=calc_doc) is False


def test_script_header_needs_data_binding_for_direct_helper_templates():
    from plugin.scripting.analysis import get_analysis_script_templates
    from plugin.scripting.duckdb_sql import get_sql_script_templates

    calc_doc = object()
    with patch("plugin.framework.doc_type.is_calc", return_value=True):
        assert ScriptFacts.from_source(
            get_analysis_script_templates()["describe_data"],
        ).needs_data_binding(doc=calc_doc) is True
        assert ScriptFacts.from_source(
            get_sql_script_templates()["query_sheet_sql"],
        ).needs_data_binding(doc=calc_doc) is True


def test_script_header_needs_data_binding_ignores_comments_and_non_calc():

    calc_doc = object()
    non_calc_doc = object()

    def is_calc_mock(doc: object) -> bool:
        return doc is calc_doc

    viz_code = "from writeragent.scripting.viz import quick_plot\nresult = quick_plot()\n"

    with patch("plugin.framework.doc_type.is_calc", side_effect=is_calc_mock):
        assert ScriptFacts.from_source("# run_analysis(foo)\n").needs_data_binding(doc=calc_doc) is False
        assert ScriptFacts.from_source('x = "run_analysis("\n').needs_data_binding(doc=calc_doc) is False
        assert ScriptFacts.from_source("run_analysis()\n").needs_data_binding(doc=calc_doc) is True
        assert (
            ScriptFacts.from_source(
                "result = writeragent.scripting.analysis.run_analysis()\n",
            ).needs_data_binding(doc=calc_doc)
            is True
        )
        assert ScriptFacts.from_source("query_folder_sql()\n").needs_data_binding(doc=calc_doc) is True
        assert (
            ScriptFacts.from_source(
                "import writeragent.scripting.analysis as analysis\nresult = analysis.describe_data()\n",
            ).needs_data_binding(doc=calc_doc)
            is True
        )
        assert (
            ScriptFacts.from_source(
                "import writeragent.scripting.analysis\nresult = writeragent.scripting.analysis.describe_data()\n",
            ).needs_data_binding(doc=calc_doc)
            is False
        )
        assert ScriptFacts.from_source("run_analysis(\n").needs_data_binding(doc=calc_doc) is True
        assert ScriptFacts.from_source("run_viz()\n").needs_data_binding(doc=non_calc_doc) is False
        assert ScriptFacts.from_source(viz_code).needs_data_binding(doc=non_calc_doc) is False
        assert ScriptFacts.from_source(viz_code).needs_data_binding(doc=calc_doc) is True




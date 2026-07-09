# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for shared helper-domain header/template glue."""

from __future__ import annotations

from plugin.scripting.helper_domain import (
    build_helper_script_template,
    format_elapsed_time,
    parse_helper_script_header,
    parse_run_import_call_params,
    parse_run_import_call_spec,
)


def test_parse_valid_header():
    code = '# writeragent:units helper=convert_quantity params={"value":"10"}\nresult = 1\n'
    meta = parse_helper_script_header(code, tag="units", helper_names={"convert_quantity"})
    assert meta is not None
    assert meta.helper == "convert_quantity"
    assert meta.params == {"value": "10"}


def test_parse_missing_tag():
    code = '# writeragent:analysis helper=describe_data params={}\n'
    assert parse_helper_script_header(code, tag="units", helper_names={"convert_quantity"}) is None


def test_parse_unknown_helper():
    code = "# writeragent:analysis helper=not_real params={}\n"
    assert parse_helper_script_header(code, tag="analysis", helper_names={"describe_data"}) is None


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
        params={"value": "10"},
        description="Convert",
        style="run_import",
        import_module="writeragent.scripting.units",
        run_name="run_units",
        data_expr="None",
        extra_comment_lines=("# Edit the run call below, then Run.",),
    )
    assert not body.startswith("# writeragent:")
    assert body.startswith("# Convert")
    assert '"value":"10"' in body
    assert "from writeragent.scripting.units import run_units" in body
    assert "result = run_units(" in body


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

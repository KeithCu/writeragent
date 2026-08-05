# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis verification for helper_domain, trusted_action_registry, and duckdb_sql."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.scripting.helper_domain import (
    HelperScriptMeta,
    header_prefix,
    parse_helper_script_header,
    parse_run_import_call_spec,
    parse_run_import_call_params,
)
from plugin.scripting.trusted_action_registry import (
    TrustedActionWiring,
    get_trusted_action_wiring,
)
from plugin.scripting.duckdb_sql import (
    SqlScriptMeta,
    get_sql_script_templates,
    parse_sql_script_header,
    SQL_HELPER_NAMES,
)


@given(tag=st.text(min_size=1, max_size=20))
@settings(max_examples=50)
def test_header_prefix_format(tag: str) -> None:
    prefix = header_prefix(tag)
    assert isinstance(prefix, str)
    assert prefix.startswith("# writeragent:")


def test_sql_script_templates_and_header_parsing() -> None:
    templates = get_sql_script_templates()
    assert isinstance(templates, dict)
    assert "query_folder_sql" in templates
    assert "query_sheet_sql" in templates

    for helper, code in templates.items():
        assert isinstance(code, str)
        meta = parse_sql_script_header(code)
        assert isinstance(meta, SqlScriptMeta)
        assert meta.helper == helper
        assert isinstance(meta.params, dict)


@given(domain=st.sampled_from(["units", "symbolic", "viz", "analysis", "sql", "embedding", "invalid_domain", ""]))
def test_trusted_action_registry_lookup(domain: str) -> None:
    wiring = get_trusted_action_wiring(domain)
    if domain in ("units", "symbolic", "viz", "analysis", "sql", "embedding"):
        assert isinstance(wiring, TrustedActionWiring)
        assert wiring.domain == domain
        assert ":" in wiring.handler
    else:
        assert wiring is None


def test_parse_run_import_call_spec_literal() -> None:
    code1 = 'run_sql({"helper": "query_folder_sql", "params": {"files": ["a.csv"]}})'
    spec1 = parse_run_import_call_spec(code1, run_name="run_sql")
    assert isinstance(spec1, dict)
    assert spec1.get("helper") == "query_folder_sql"
    assert spec1.get("params") == {"files": ["a.csv"]}

    params1 = parse_run_import_call_params(code1, run_name="run_sql")
    assert params1 == {"files": ["a.csv"]}


from plugin.scripting.audio_silence_detector import pcm_energy_int16
from plugin.calc.cells import _parse_color


@given(pcm=st.binary(max_size=200))
def test_pcm_energy_int16_invariants(pcm: bytes) -> None:
    rms, peak = pcm_energy_int16(pcm)
    assert isinstance(rms, float)
    assert isinstance(peak, float)
    assert 0.0 <= rms <= 1.0
    assert 0.0 <= peak <= 1.0


@given(code=st.text(max_size=100), run_name=st.sampled_from(["run_sql", "run_units", "run_analysis", "invalid_run"]))
def test_parse_run_import_call_spec_no_crash(code: str, run_name: str) -> None:
    res = parse_run_import_call_spec(code, run_name=run_name)
    assert res is None or isinstance(res, dict)


@given(color_str=st.one_of(st.text(max_size=30), st.none()))
def test_parse_color_invariants(color_str: str | None) -> None:
    res = _parse_color(color_str)
    if res is not None:
        assert isinstance(res, int)
        assert 0 <= res <= 0xFFFFFF


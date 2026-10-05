# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Structural tests for the trusted-helper domain registry."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from plugin.scripting.domain_registry import (
    POST_VENV_DOMAIN_ORDER,
    WIRING_TABLE,
    _RUN_IMPORT_DATA_BINDING,
    get_picker_domains,
    get_post_venv_domains,
    get_rps_domains,
)


def test_wiring_table_uses_colon_module_attr():
    for w in WIRING_TABLE:
        for path in (w.insert, w.is_result):
            assert path.count(":") == 1, path
            mod_name, attr_name = path.split(":")
            assert "." in mod_name
            assert attr_name.isidentifier()


def test_rps_domain_order():
    ids = [s.id for s in get_rps_domains()]
    assert ids == [
        "vision",
        "viz",
        "math",
        "units",
        "text",
        "quant",
        "optimize",
        "forecast",
        "analysis",
        "sql",
    ]


def test_rps_domains_have_post_venv_hooks():
    for spec in get_rps_domains():
        assert callable(spec.insert)
        assert callable(spec.format_ok)
        assert callable(spec.is_result)


def test_post_venv_order_matches_constant():
    ids = [s.id for s in get_post_venv_domains()]
    assert ids == list(POST_VENV_DOMAIN_ORDER)


def test_wiring_and_post_venv_and_run_import_binding_agree():
    """Same domain ids in WIRING_TABLE and POST_VENV_DOMAIN_ORDER; calc_only flags match.

    Picker is not 1:1 (text is wiring-only) — do not assert that.
    Order of the two wiring lists is intentionally different; only membership is shared.
    """
    wiring_ids = {w.id for w in WIRING_TABLE}
    assert set(POST_VENV_DOMAIN_ORDER) == wiring_ids
    by_id = {w.id: w for w in WIRING_TABLE}
    for run_name, cfg in _RUN_IMPORT_DATA_BINDING.items():
        assert run_name.startswith("run_")
        domain_id = run_name[len("run_") :]
        assert domain_id in by_id
        assert cfg.get("calc_only") is by_id[domain_id].post_venv_calc_only


def test_script_header_needs_data_binding_on_calc_domains():
    from plugin.scripting.domain_registry import script_header_needs_data_binding

    calc_doc = object()
    with patch("plugin.scripting.domain_registry.is_calc", return_value=True):
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
    from plugin.scripting.domain_registry import script_header_needs_data_binding
    from plugin.scripting.duckdb_sql import get_sql_script_templates

    calc_doc = object()
    with patch("plugin.scripting.domain_registry.is_calc", return_value=True):
        assert script_header_needs_data_binding(
            get_analysis_script_templates()["describe_data"],
            doc=calc_doc,
        ) is True
        assert script_header_needs_data_binding(
            get_sql_script_templates()["query_sheet_sql"],
            doc=calc_doc,
        ) is True


def test_picker_domains_are_cached():
    assert get_picker_domains() is get_picker_domains()


def test_calc_only_insert_passes_document_then_context():
    """Calc egress is (doc, ctx, result). Swapping those args inserts into the wrong object."""
    from plugin.scripting.domain_registry import build_rps_spec

    ctx, doc = object(), object()
    result = {"helper": "x"}
    for wiring in WIRING_TABLE:
        seen: dict[str, tuple[object, ...]] = {}

        def _record(*args: object, **_kwargs: object) -> int:
            seen["args"] = args
            return 2

        spec = build_rps_spec(wiring)
        assert spec.insert is not None
        with patch("plugin.scripting.domain_registry._resolve_fn", return_value=_record):
            spec.insert(ctx, doc, result)
        if wiring.post_venv_calc_only:
            assert seen["args"] == (doc, ctx, result), wiring.id
        else:
            assert seen["args"] == (ctx, doc, result), wiring.id


def test_vision_html_block_count_ignores_tag_prefixes():
    from plugin.scripting.domain_registry import build_rps_spec

    wiring = next(w for w in WIRING_TABLE if w.id == "vision")
    spec = build_rps_spec(wiring)
    html = (
        "<html><head><title>t</title></head><header></header><hr>"
        "<pre>no</pre><param name='p'/><p>a</p><p class='x'>b</p>"
        "<h2>c</h2><table><tr><td>d</td></tr></table>"
    )
    assert spec.format_ok is not None
    out = spec.format_ok(
        meta=SimpleNamespace(helper="extract_structure"),
        result={"helper": "extract_structure", "html": html, "metrics": {"table_count": 1}},
        t0=0.0,
    )
    assert "4 blocks" in out["status_ok_text"]
    assert "1 tables" in out["status_ok_text"]


def test_picker_domains_unique_origins_and_prefixes():
    domains = get_picker_domains()
    origins = [d.origin for d in domains]
    prefixes = [d.display_prefix for d in domains]
    assert len(origins) == len(set(origins))
    assert len(prefixes) == len(set(prefixes))
    for d in domains:
        assert d.display_prefix
        assert callable(d.supports)
        assert callable(d.templates)
        assert callable(d.title_fn)


def test_picker_order_starts_with_vision_math_units_analysis():
    origins = [d.origin for d in get_picker_domains()]
    assert origins[:4] == ["vision", "math", "units", "analysis"]
    assert origins == [
        "vision",
        "math",
        "units",
        "analysis",
        "sql",
        "viz",
        "quant",
        "optimize",
        "forecast",
    ]


def test_picker_supports_logs_when_the_check_raises(caplog):
    # What was wrong: logger.debug calls are stripped in release builds.
    # Why: skip when running against stripped release bundle.
    from tests.harness.strip_bundle import skip_if_release_build

    skip_if_release_build("log.debug stripped in release bundle")
    import logging

    from plugin.scripting.domain_registry import _picker_supports_fn

    fn = _picker_supports_fn("plugin.scripting.domain_registry:does_not_exist")
    with caplog.at_level(logging.DEBUG, logger="writeragent.scripting"):
        assert fn(object()) is False
    assert "supports check failed" in caplog.text

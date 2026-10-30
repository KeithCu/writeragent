# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Structural tests for the trusted-helper domain registry."""

from __future__ import annotations

from types import SimpleNamespace
from typing import get_args
from unittest.mock import patch

from plugin.scripting.domain_registry import (
    DOC_SCRIPT_DISPLAY_PREFIX,
    PICKER_WIRING,
    POST_VENV_DOMAIN_ORDER,
    WIRING_TABLE,
    FormatOkKind,
    get_picker_domains,
    get_post_venv_domains,
)
from plugin.scripting.helper_domain import _RUN_DATA_BINDING_CALLS


def test_registry_module_attr_targets_resolve():
    # These modules import without a live LibreOffice (vision_egress, vision_runner,
    # vision_templates, viz, symbolic, units, text_analytics, quant_egress,
    # optimize, forecast, analysis_egress, duckdb_sql, analysis).
    from plugin.scripting.domain_registry import _resolve_module_attr

    targets: list[str] = []
    for w in WIRING_TABLE:
        targets.extend([w.insert, w.is_result])
        if w.insert_kwargs_from_code is not None:
            targets.append(w.insert_kwargs_from_code)
    for p in PICKER_WIRING:
        targets.append(p.templates_ref)
        if p.supports_ref != "calc_only":
            targets.append(p.supports_ref)

    for target in targets:
        assert target.count(":") == 1, target
        mod_name, attr_name = target.split(":")
        assert "." in mod_name, target
        assert attr_name.isidentifier(), target
        fn = _resolve_module_attr(target)
        assert callable(fn), target


def test_rps_domain_order():
    ids = [w.id for w in WIRING_TABLE]
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
    for spec in get_post_venv_domains():
        assert callable(spec.insert)
        assert callable(spec.format_ok)
        assert callable(spec.is_result)


def test_post_venv_order_matches_constant():
    ids = [s.id for s in get_post_venv_domains()]
    assert ids == list(POST_VENV_DOMAIN_ORDER)


def test_wiring_and_post_venv_and_run_import_binding_agree():
    """Same domain ids in WIRING_TABLE and POST_VENV_DOMAIN_ORDER.

    Picker is not 1:1 (text is wiring-only) — do not assert that.
    Order of the two wiring lists is intentionally different; only membership is shared.
    The toolbar is Calc-only for every name, including run_viz, so it is not compared to post_venv_calc_only.
    """
    wiring_ids = {w.id for w in WIRING_TABLE}
    assert set(POST_VENV_DOMAIN_ORDER) == wiring_ids
    for run_name in _RUN_DATA_BINDING_CALLS:
        assert run_name.startswith("run_")
        domain_id = run_name[len("run_") :]
        assert domain_id in wiring_ids


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
        with patch("plugin.scripting.domain_registry._resolve_module_attr", return_value=_record):
            spec.insert(ctx, doc, result)
        if wiring.post_venv_calc_only:
            assert seen["args"] == (doc, ctx, result), wiring.id
        else:
            assert seen["args"] == (ctx, doc, result), wiring.id


def test_calc_only_insert_forwards_kwargs():
    from plugin.scripting.domain_registry import build_rps_spec, WIRING_TABLE

    wiring = next(w for w in WIRING_TABLE if w.post_venv_calc_only)
    ctx, doc, result = object(), object(), {"helper": "x"}
    seen: dict[str, object] = {}

    def _record(*args: object, **kwargs: object) -> int:
        seen["args"] = args
        seen["kwargs"] = kwargs
        return 3

    spec = build_rps_spec(wiring)
    assert spec.insert is not None
    with patch("plugin.scripting.domain_registry._resolve_module_attr", return_value=_record):
        assert spec.insert(ctx, doc, result, start_col=4) == 3
    assert seen["args"] == (doc, ctx, result)
    assert seen["kwargs"] == {"start_col": 4}


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

    from plugin.scripting.domain_registry import PickerWiring

    wiring = PickerWiring(
        origin="test",
        display_prefix="[Test] ",
        title="Test",
        supports_ref="plugin.scripting.domain_registry:does_not_exist",
        templates_ref="",
    )
    with caplog.at_level(logging.DEBUG, logger="writeragent.scripting"):
        assert wiring.supports(object()) is False
    assert "supports check failed" in caplog.text


def test_sql_is_result_handles_missing_duckdb_sql(caplog):
    # What was wrong: LibrePy bundle excludes duckdb_sql.py, causing is_result check
    # to raise ImportError and break Calc result insertions for non-sql scripts.
    # Why this change: Catch ImportError, log at debug, and treat as not this domain (return False).
    import logging
    from plugin.scripting.domain_registry import build_rps_spec, WIRING_TABLE

    sql_wiring = next(w for w in WIRING_TABLE if w.id == "sql")
    spec = build_rps_spec(sql_wiring)

    with patch("plugin.scripting.domain_registry._resolve_module_attr", side_effect=ImportError("No module named 'plugin.scripting.duckdb_sql'")):
        with caplog.at_level(logging.DEBUG, logger="writeragent.scripting"):
            assert spec.is_result({"some": "result"}) is False
    # Release strips log.debug; returning False is the behavior that must survive.
    from plugin.scripting import domain_registry as domain_registry_mod
    from tests.harness.strip_bundle import module_source_contains

    if module_source_contains(domain_registry_mod, "is_result module unavailable"):
        assert "Domain sql is_result module unavailable" in caplog.text


def test_is_reserved_script_name():
    from plugin.scripting.domain_registry import is_reserved_script_name, RESERVED_SCRIPT_PREFIXES

    assert is_reserved_script_name("[Doc] MyScript") is True
    assert is_reserved_script_name("[Vision] extract_text") is True
    assert is_reserved_script_name("[Analysis] describe_data") is True
    assert is_reserved_script_name("MyScript") is False
    assert is_reserved_script_name("DocScript") is False
    for prefix in RESERVED_SCRIPT_PREFIXES:
        assert is_reserved_script_name(f"{prefix} test") is True
    assert RESERVED_SCRIPT_PREFIXES == (
        DOC_SCRIPT_DISPLAY_PREFIX,
        *(row.display_prefix for row in PICKER_WIRING),
    )


def test_rps_spec_insert_returns_int_only_when_int():
    # What was wrong: bool is an instance of int in Python, so returning True could
    # leak as cell count 1 or mask an integer return check.
    # Why this change: Explicitly check isinstance(ret, int) and not isinstance(ret, bool).
    from plugin.scripting.domain_registry import build_rps_spec, WIRING_TABLE

    wiring = next(w for w in WIRING_TABLE if w.id == "viz")
    spec = build_rps_spec(wiring)

    with patch("plugin.scripting.domain_registry._resolve_module_attr", return_value=lambda *a, **kw: 42):
        assert spec.insert(object(), object(), {}) == 42

    with patch("plugin.scripting.domain_registry._resolve_module_attr", return_value=lambda *a, **kw: True):
        assert spec.insert(object(), object(), {}) is None

    with patch("plugin.scripting.domain_registry._resolve_module_attr", return_value=lambda *a, **kw: None):
        assert spec.insert(object(), object(), {}) is None


def test_format_ok_kind_and_display_label():
    allowed_kinds = set(get_args(FormatOkKind))
    assert allowed_kinds == {"generic", "plot", "symbolic", "units", "vision", "rows"}
    for w in WIRING_TABLE:
        assert w.format_ok_kind in allowed_kinds
        assert isinstance(w.display_label, str) and w.display_label


def test_picker_supports_matches_post_venv_calc_only():
    by_id = {w.id: w for w in WIRING_TABLE}
    for row in PICKER_WIRING:
        assert row.origin in by_id
        if by_id[row.origin].post_venv_calc_only:
            assert row.supports_ref == "calc_only"
        else:
            assert row.supports_ref != "calc_only"
            assert row.supports_ref.count(":") == 1
    assert set(by_id) - {row.origin for row in PICKER_WIRING} == {"text"}


def test_quant_picker_is_calc_only():
    quant_spec = next(d for d in get_picker_domains() if d.origin == "quant")
    calc_doc = object()
    writer_doc = object()
    with patch("plugin.scripting.domain_registry.is_calc", side_effect=lambda doc: doc is calc_doc):
        assert quant_spec.supports(calc_doc) is True
        assert quant_spec.supports(writer_doc) is False
        assert quant_spec.supports(None) is False


def test_units_format_ok_joins_formatted_list():
    from plugin.scripting.domain_registry import build_rps_spec

    wiring = next(w for w in WIRING_TABLE if w.id == "units")
    spec = build_rps_spec(wiring)
    assert spec.format_ok is not None
    out = spec.format_ok(
        meta=SimpleNamespace(helper="convert_quantity"),
        result={
            "helper": "convert_quantity",
            "formatted": ["36 km/h", "72 km/h"],
            "text": "2 quantities converted",
        },
        t0=0.0,
    )
    assert "36 km/h, 72 km/h" in out["status_ok_text"]
    assert "['36 km/h'" not in out["status_ok_text"]



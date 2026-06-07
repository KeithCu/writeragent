# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for viz Run Python Script templates."""

from __future__ import annotations

from plugin.scripting.viz_templates import get_viz_script_templates, parse_viz_script_header


def test_get_viz_script_templates_include_header():
    templates = get_viz_script_templates()
    assert "quick_plot" in templates
    assert "# writeragent:viz" in templates["quick_plot"]
    assert "run_viz" in templates["quick_plot"]


def test_parse_viz_script_header_round_trip():
    code = get_viz_script_templates()["correlation_heatmap"]
    meta = parse_viz_script_header(code)
    assert meta is not None
    assert meta.helper == "correlation_heatmap"
    assert meta.params.get("method") == "pearson"


def test_parse_viz_script_header_rejects_unknown_helper():
    code = "# writeragent:viz helper=not_a_helper params={}\n"
    assert parse_viz_script_header(code) is None

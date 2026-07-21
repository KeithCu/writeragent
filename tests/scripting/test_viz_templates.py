# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for viz Run Python Script templates."""

from __future__ import annotations

from plugin.scripting.viz import get_viz_script_templates, parse_viz_script_header


def test_get_viz_script_templates_include_run_call():
    templates = get_viz_script_templates()
    assert "quick_plot" in templates
    assert "from writeragent.scripting.viz import quick_plot" in templates["quick_plot"]
    assert "# writeragent:viz" not in templates["quick_plot"]


def test_viz_template_body_includes_helper_params():
    code = get_viz_script_templates()["correlation_heatmap"]
    assert 'correlation_heatmap(method="pearson")' in code
    assert "from writeragent.scripting.viz import correlation_heatmap" in code


def test_parse_viz_script_header_rejects_unknown_helper():
    code = "# writeragent:viz helper=not_a_helper params={}\n"
    assert parse_viz_script_header(code) is None

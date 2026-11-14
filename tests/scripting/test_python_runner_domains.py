# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Run Python Script domain venv execution and post-venv result routing."""

from __future__ import annotations

from unittest.mock import ANY, MagicMock, patch

import pytest

from plugin.scripting.domain_registry import get_domain_wiring
from tests.harness.strip_bundle import requires_asserts
from tests.scripting.python_runner_test_support import execute_and_insert_result

_SAMPLE_IMAGE = {"__wa_payload__": "image", "format": "png", "data": b"abc"}


@pytest.mark.parametrize(
    ("domain", "helper", "insert_target", "is_calc_doc", "data_range", "venv_result", "expected_status", "expected_bindings", "check_doc_ctx_order"),
    [
        (
            "text",
            "full",
            "plugin.scripting.text_analytics.insert_text_analytics_result_into_doc",
            False,
            None,
            {"status": "ok", "result": {"readability": {"flesch_reading_ease": 60}, "entities": []}},
            "Text analytics",
            {"text": "Sample document text.", "document_context": {}},
            False,
        ),
        (
            "units",
            "convert_quantity",
            "plugin.scripting.units.insert_units_result_into_doc",
            False,
            None,
            {"status": "ok", "helper": "convert_quantity", "formatted": "36 kilometer / hour", "text": "36 kilometer / hour", "magnitude": 36.0},
            "convert_quantity",
            None,
            False,
        ),
        (
            "viz",
            "quick_plot",
            "plugin.scripting.viz.insert_viz_result_into_doc",
            True,
            "Sheet1.A1:B5",
            {"status": "ok", "helper": "quick_plot", "title": "Quick plot: Sales", "image": _SAMPLE_IMAGE},
            "Plot inserted",
            None,
            False,
        ),
        (
            "analysis",
            "describe_data",
            "plugin.calc.analysis_egress.insert_analysis_result_into_calc",
            True,
            "Sheet1.A1:B2",
            {"status": "ok", "helper": "describe_data", "metrics": {"row_count": 1}},
            "describe_data",
            None,
            True,
        ),
    ],
)
def test_execute_and_insert_domain_skips_fast_path(
    domain, helper, insert_target, is_calc_doc, data_range, venv_result, expected_status, expected_bindings, check_doc_ctx_order
):
    ctx = MagicMock()
    doc = MagicMock()
    doc.getCurrentController.return_value = MagicMock()
    doc.getCurrentController.return_value.getSelection.return_value = None

    with (
        patch(insert_target) as mock_insert,
        patch("plugin.scripting.helper_domain.run_domain") as mock_run,
        patch("plugin.scripting.python_runner.run_code_in_user_venv", return_value={"status": "ok", "result": venv_result}) as mock_venv,
        # MagicMock doc has no int range address; read_range is not under test here.
        patch("plugin.calc.inspector.CellInspector.read_range", return_value=[]),
        patch("plugin.scripting.python_runner.is_writer", return_value=not is_calc_doc),
        patch("plugin.scripting.python_runner.is_calc", return_value=is_calc_doc),
        patch("plugin.scripting.python_runner.resolve_text_analytics_document_inputs", return_value=("Sample document text.", {})),
    ):
        mock_insert.return_value = 5
        code = get_domain_wiring(domain).templates()[helper]
        outcome = execute_and_insert_result(ctx, doc, code, data_range=data_range)

    assert outcome["ok"] is True
    assert expected_status in outcome["status_ok_text"]
    mock_run.assert_not_called()
    mock_venv.assert_called_once()
    mock_insert.assert_called_once()
    assert mock_venv.call_args.kwargs["bindings"] == expected_bindings
    assert (not check_doc_ctx_order) or (mock_insert.call_args.args[:2] == (doc, ctx))


@pytest.mark.parametrize(
    ("insert_target", "is_calc_doc", "code", "venv_result", "expected_status", "check_doc_ctx_order"),
    [
        (
            "plugin.scripting.text_analytics.insert_text_analytics_result_into_doc",
            False,
            "from writeragent.scripting.text_analytics import run_text_analytics\nresult = ...",
            {"status": "ok", "result": {"key_phrases": [{"text": "budget review", "lemma": "budget review"}]}},
            "Text analytics",
            False,
        ),
        (
            "plugin.scripting.units.insert_units_result_into_doc",
            False,
            "from plugin.scripting.units import run_units\nresult = ...",
            {"status": "ok", "helper": "parse_quantity", "formatted": "10 meter / second", "text": "10 meter / second", "magnitude": 10.0},
            "parse_quantity",
            False,
        ),
        (
            "plugin.scripting.viz.insert_viz_result_into_doc",
            True,
            "from plugin.scripting.viz import run_viz\nresult = run_viz(...)",
            {"status": "ok", "helper": "plot_data", "title": "Scatter", "image": _SAMPLE_IMAGE},
            "plot_data",
            False,
        ),
        (
            "plugin.calc.analysis_egress.insert_analysis_result_into_calc",
            True,
            "result = {'status': 'ok', 'helper': 'quick_stats', 'metrics': {}}",
            {"status": "ok", "helper": "quick_stats", "metrics": {"rows": 3}},
            "quick_stats",
            True,
        ),
    ],
)
def test_execute_and_insert_detects_domain_result_from_venv(
    insert_target, is_calc_doc, code, venv_result, expected_status, check_doc_ctx_order
):
    ctx = MagicMock()
    doc = MagicMock()

    with (
        patch(insert_target) as mock_insert,
        patch("plugin.scripting.python_runner.run_code_in_user_venv", return_value={"status": "ok", "result": venv_result}),
        patch("plugin.scripting.python_runner.is_writer", return_value=not is_calc_doc),
        patch("plugin.scripting.python_runner.is_calc", return_value=is_calc_doc),
    ):
        mock_insert.return_value = 4
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    assert expected_status in outcome["status_ok_text"]
    mock_insert.assert_called_once()
    if check_doc_ctx_order:
        assert mock_insert.call_args.args[:2] == (doc, ctx)


@patch("plugin.scripting.text_analytics.insert_text_analytics_result_into_doc")
@patch("plugin.scripting.python_runner.run_code_in_user_venv")
def test_topics_and_sentiment_bind_section_list(mock_venv, mock_insert):
    """Section lists must be JSON arrays, not the repr of the list."""
    ctx = MagicMock()
    doc = MagicMock()
    sections = ["Section one about revenue.", "Section two about budgets."]
    mock_venv.return_value = {
        "status": "ok",
        "result": {"status": "ok", "result": {"topics": [], "sentiment": {}}},
    }
    for helper in ("topics", "sentiment"):
        mock_venv.reset_mock()
        code = get_domain_wiring("text").templates()[helper]
        with (
            patch("plugin.scripting.python_runner.is_writer", return_value=True),
            patch(
                "plugin.scripting.python_runner.resolve_text_analytics_document_inputs",
                return_value=(sections, {"lang": "en"}),
            ),
        ):
            outcome = execute_and_insert_result(ctx, doc, code)
        assert outcome["ok"] is True
        assert mock_venv.call_args.args[0] == code
        assert mock_venv.call_args.kwargs["bindings"] == {
            "text": sections,
            "document_context": {"lang": "en"},
        }


@requires_asserts
def test_text_analytics_input_type_assertions():
    from plugin.scripting.python_runner import _prepare_rps_execution

    ctx = MagicMock()
    doc = MagicMock()
    code = get_domain_wiring("text").templates()["full"]

    with patch("plugin.scripting.python_runner.is_writer", return_value=True):
        with patch("plugin.scripting.python_runner.resolve_text_analytics_document_inputs", return_value=(123, {})):
            with pytest.raises(AssertionError):
                _prepare_rps_execution(ctx, doc, code)

        with patch("plugin.scripting.python_runner.resolve_text_analytics_document_inputs", return_value=("valid text", "not a dict")):
            with pytest.raises(AssertionError):
                _prepare_rps_execution(ctx, doc, code)


@patch("plugin.scripting.units.insert_units_result_into_doc")
@patch("plugin.scripting.python_runner.run_code_in_user_venv")
def test_execute_and_insert_units_passes_output_style_from_body(mock_venv, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    code = (
        "from writeragent.scripting.units import run_units\n"
        'result = run_units({"helper": "convert_quantity", "params": {"value":"10","from_unit":"m/s","to_unit":"km/h","output_style":"detailed"}}, None, {})\n'
    )
    with patch("plugin.scripting.python_runner.is_writer", return_value=True):
        mock_venv.return_value = {
            "status": "ok",
            "result": {"status": "ok", "helper": "convert_quantity", "formatted": "36 km/h", "text": "36 km/h"},
        }
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    mock_insert.assert_called_once()
    assert mock_insert.call_args.kwargs.get("output_style") == "detailed"


@patch("plugin.scripting.units.insert_units_result_into_doc")
@patch("plugin.scripting.python_runner.run_code_in_user_venv")
def test_execute_and_insert_units_uses_body_params_not_header(mock_venv, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    code = (
        "from writeragent.scripting.units import run_units\n"
        'result = run_units({"helper": "convert_quantity", "params": {"value":"20","from_unit":"m/s","to_unit":"mm/h"}}, None, {})\n'
    )
    with patch("plugin.scripting.python_runner.is_writer", return_value=True):
        mock_venv.return_value = {
            "status": "ok",
            "result": {"status": "ok", "helper": "convert_quantity", "formatted": "72000000 millimeter / hour", "text": "72000000 millimeter / hour"},
        }
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    mock_venv.assert_called_once_with(code, data=None, bindings=None, session_id=None, script_session_id=ANY)
    assert "millimeter" in mock_insert.call_args.args[2].get("formatted", "")


@patch("plugin.scripting.python_runner.try_insert_plot_result")
@patch("plugin.scripting.python_runner.run_code_in_user_venv")
def test_execute_and_insert_detects_raw_image_from_venv(mock_venv, mock_try_insert):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_calc", return_value=True):
        mock_venv.return_value = {"status": "ok", "result": _SAMPLE_IMAGE}
        mock_try_insert.return_value = True
        outcome = execute_and_insert_result(ctx, doc, "result = plt.plot([1,2,3])")

    assert outcome["ok"] is True
    assert "Plot inserted" in outcome["status_ok_text"]
    mock_try_insert.assert_called_once()

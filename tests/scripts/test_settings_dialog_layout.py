# WriterAgent tests for SettingsDialog XDL layout generation
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from manifest_registry import generate_settings_dialog_tabs  # noqa: E402

_DLG_NS = "http://openoffice.org/2000/dialog"


def _control_tops(xdl_path: Path) -> dict[str, str]:
    """Map control dlg:id -> dlg:top from generated SettingsDialog XDL."""
    root = ET.parse(xdl_path).getroot()
    tops: dict[str, str] = {}
    for el in root.iter():
        ctrl_id = el.get(f"{{{_DLG_NS}}}id")
        top = el.get(f"{{{_DLG_NS}}}top")
        if ctrl_id and top and ctrl_id not in tops:
            tops[ctrl_id] = top
    return tops


def _same_layout_row(tops: dict[str, str], left_id: str, right_id: str) -> bool:
    """True when controls share a row (checkbox/label tops may be +2 vs field tops)."""
    return abs(int(tops[left_id]) - int(tops[right_id])) <= 2


def _generate_settings_xdl(tmp_path: Path) -> tuple[Path, str]:
    from plugin._manifest import MODULES

    tpl = _REPO / "extension" / "WriterAgentDialogs" / "SettingsDialog.xdl.tpl"
    out = tmp_path / "SettingsDialog.xdl"
    generate_settings_dialog_tabs(MODULES, str(tpl), str(out))
    assert out.is_file(), "generate_settings_dialog_tabs did not write output"
    return out, out.read_text(encoding="utf-8")


def test_chatbot_selection_token_fields_absent_from_settings(tmp_path: Path) -> None:
    xdl_path, _xdl = _generate_settings_xdl(tmp_path)
    tops = _control_tops(xdl_path)

    assert "chatbot__extend_selection_max_tokens" not in tops
    assert "chatbot__edit_selection_max_new_tokens" not in tops


def test_chatbot_paired_checkbox_fields_share_row(tmp_path: Path) -> None:
    xdl_path, _xdl = _generate_settings_xdl(tmp_path)
    tops = _control_tops(xdl_path)

    assert tops["chatbot__web_research_cache_enabled"] == tops["chatbot__prompt_for_web_research"]


def test_chatbot_paired_cache_fields_share_row(tmp_path: Path) -> None:
    xdl_path, _xdl = _generate_settings_xdl(tmp_path)
    tops = _control_tops(xdl_path)

    assert tops["chatbot__web_cache_max_mb"] == tops["chatbot__web_cache_validity_days"]


def test_web_research_cache_before_web_cache_size_controls(tmp_path: Path) -> None:
    _xdl_path, xdl = _generate_settings_xdl(tmp_path)

    cache_idx = xdl.index('dlg:id="chatbot__web_research_cache_enabled"')
    max_mb_idx = xdl.index('dlg:id="chatbot__web_cache_max_mb"')
    assert cache_idx < max_mb_idx


def test_right_column_number_labels_use_label_x(tmp_path: Path) -> None:
    _xdl_path, xdl = _generate_settings_xdl(tmp_path)

    assert re.search(
        r'dlg:id="label_chatbot__web_cache_validity_days"[^>]*dlg:left="220"',
        xdl,
    )


def test_doc_grammar_enable_and_model_share_row(tmp_path: Path) -> None:
    xdl_path, _xdl = _generate_settings_xdl(tmp_path)
    tops = _control_tops(xdl_path)

    assert _same_layout_row(tops, "doc__grammar_proofreader_enabled", "doc__grammar_proofreader_model")


def test_doc_batch_sentences_and_concurrent_share_row(tmp_path: Path) -> None:
    xdl_path, _xdl = _generate_settings_xdl(tmp_path)
    tops = _control_tops(xdl_path)

    assert tops["doc__grammar_proofreader_batch_sentences"] == tops["doc__grammar_proofreader_max_in_flight"]


def test_doc_model_label_uses_right_column(tmp_path: Path) -> None:
    _xdl_path, xdl = _generate_settings_xdl(tmp_path)

    assert re.search(
        r'dlg:id="label_doc__grammar_proofreader_model"[^>]*dlg:left="220"',
        xdl,
    )


def test_json_only_settings_absent_from_settings_xdl(tmp_path: Path) -> None:
    """Internal module.yaml keys are not emitted as Settings dialog controls."""
    xdl_path, _xdl = _generate_settings_xdl(tmp_path)
    tops = _control_tops(xdl_path)

    for hidden_id in (
        "mcp__cors_allow_private_origins",
        "scripting__native_run_script_modeless",
        "scripting__force_internal_script_editor",
        "chatbot__show_search_thinking",
        "chatbot__extend_selection_max_tokens",
        "chatbot__edit_selection_max_new_tokens",
    ):
        assert hidden_id not in tops

    assert "mcp__mcp_enabled" in tops
    assert "scripting__python_venv_path" in tops

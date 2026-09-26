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

from manifest_registry import _settings_hover_text, generate_settings_dialog_tabs  # noqa: E402

_DLG_NS = "http://openoffice.org/2000/dialog"


def _control_attrs(xdl_path: Path) -> dict[str, dict[str, str]]:
    """Map control dlg:id -> {top,left,width,height} from generated SettingsDialog XDL."""
    root = ET.parse(xdl_path).getroot()
    attrs: dict[str, dict[str, str]] = {}
    for el in root.iter():
        ctrl_id = el.get(f"{{{_DLG_NS}}}id")
        if not ctrl_id or ctrl_id in attrs:
            continue
        attrs[ctrl_id] = {
            "top": el.get(f"{{{_DLG_NS}}}top") or "",
            "left": el.get(f"{{{_DLG_NS}}}left") or "",
            "width": el.get(f"{{{_DLG_NS}}}width") or "",
            "height": el.get(f"{{{_DLG_NS}}}height") or "",
        }
    return attrs


def _control_tops(xdl_path: Path) -> dict[str, str]:
    """Map control dlg:id -> dlg:top from generated SettingsDialog XDL."""
    return {cid: vals["top"] for cid, vals in _control_attrs(xdl_path).items() if vals["top"]}


def _same_layout_row(tops: dict[str, str], left_id: str, right_id: str) -> bool:
    """True when controls share a row (checkbox/label tops may be +2 vs field tops)."""
    return abs(int(tops[left_id]) - int(tops[right_id])) <= 2


def _generate_settings_xdl(tmp_path: Path) -> tuple[Path, str]:
    from plugin._manifest import MODULES

    tpl = _REPO / "extension" / "Dialogs" / "SettingsDialog.xdl.tpl"
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


def test_doc_recheck_and_pause_share_row_below_checker(tmp_path: Path) -> None:
    xdl_path, xdl = _generate_settings_xdl(tmp_path)
    tops = _control_tops(xdl_path)
    attrs = _control_attrs(xdl_path)

    assert _same_layout_row(tops, "doc__grammar_proofreader_recheck", "doc__grammar_proofreader_pause_during_agent")
    assert int(tops["doc__grammar_proofreader_recheck"]) > int(tops["doc__grammar_proofreader_enabled"])
    assert attrs["doc__grammar_proofreader_pause_during_agent"]["left"] == "220"
    assert re.search(
        r'dlg:id="label_doc__grammar_proofreader_recheck"',
        xdl,
    )
    assert re.search(
        r'dlg:id="doc__grammar_proofreader_recheck"[^>]*dlg:width="70"',
        xdl,
    )


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


def test_settings_tab_buttons_in_user_facing_order(tmp_path: Path) -> None:
    """Module tabs follow SETTINGS_TAB_MODULE_ORDER, not manifest/topo-sort order."""
    _xdl_path, xdl = _generate_settings_xdl(tmp_path)

    tab_ids = (
        "btn_tab_audio",
        "btn_tab_doc",
        "btn_tab_chatbot",
        "btn_tab_embeddings",
        "btn_tab_mcp",
        "btn_tab_scripting",
    )
    indices = [xdl.index(f'dlg:id="{tab_id}"') for tab_id in tab_ids]
    assert indices == sorted(indices), f"tab order indices {dict(zip(tab_ids, indices, strict=True))}"
    # Speech is the first module tab, immediately after Image Settings.
    assert xdl.index('dlg:id="btn_tab_image"') < indices[0] < indices[1]


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
    assert "scripting__xl_static_rewrite" in tops
    assert "scripting__python_geometric_recalc_order" in tops


def test_xl_static_rewrite_checkbox_beside_auto_spill(tmp_path: Path) -> None:
    """Rewrite xl() shares a row with auto-spill; second control starts at dialog midpoint."""
    xdl_path, _xdl = _generate_settings_xdl(tmp_path)
    tops = _control_tops(xdl_path)
    assert _same_layout_row(tops, "scripting__python_auto_spill", "scripting__xl_static_rewrite")

    tree = ET.parse(xdl_path)
    root = tree.getroot()
    ns = {"dlg": _DLG_NS}
    lefts = {
        el.get(f"{{{_DLG_NS}}}id"): el.get(f"{{{_DLG_NS}}}left")
        for el in root.findall(".//dlg:checkbox", ns)
    }
    assert lefts.get("scripting__python_auto_spill") == "8"
    assert lefts.get("scripting__xl_static_rewrite") == "220"


def test_librepy_flavor_omits_ppt_master_from_scripting_page(tmp_path: Path) -> None:
    modules = [
        {
            "name": "scripting",
            "title": "Python",
            "config": {
                "python_venv_path": {"type": "string", "widget": "text", "label": "Python venv path"},
                "ppt_master_data_path": {
                    "type": "string",
                    "widget": "folder",
                    "label": "PPT-Master data path",
                    "librepy_exclude": True,
                },
                "test_ppt_master_data": {
                    "type": "string",
                    "widget": "button",
                    "label": "Test",
                    "librepy_exclude": True,
                },
            },
        }
    ]
    tpl = _REPO / "extension" / "Dialogs" / "SettingsDialog.xdl.tpl"
    out = tmp_path / "SettingsDialog-librepy.xdl"
    generate_settings_dialog_tabs(modules, str(tpl), str(out), librepy_flavor=True)
    tops = _control_tops(out)

    assert "scripting__python_venv_path" in tops
    assert "scripting__ppt_master_data_path" not in tops
    assert "scripting__test_ppt_master_data" not in tops


def test_audio_model_moved_to_speech_tab(tmp_path: Path) -> None:
    """General no longer has stt_model; Speech tab has audio__stt_model and rows closed up."""
    xdl_path, xdl = _generate_settings_xdl(tmp_path)
    tops = _control_tops(xdl_path)
    attrs = _control_attrs(xdl_path)

    assert "stt_model" not in tops
    assert "label_stt_model" not in tops
    assert "audio__stt_model" in tops
    assert 'dlg:id="btn_tab_audio"' in xdl
    assert 'dlg:value="Speech"' in xdl
    # One row under Image Model (combo top 72); the old Audio Model row was 88.
    assert tops["temperature"] == "88"
    assert tops["label_temperature"] == "90"
    assert tops["additional_instructions"] == "104"
    assert attrs["btn_openrouter"]["top"] == "121"
    assert tops["btn_edit_config_json"] == "140"


def test_speech_tab_voice_select_aligns_and_test_voice_is_beside(tmp_path: Path) -> None:
    """Voice matches the other Speech selects; Test voice is on that row, past the column."""
    xdl_path, xdl = _generate_settings_xdl(tmp_path)
    attrs = _control_attrs(xdl_path)

    voice = attrs["audio__tts_voice"]
    for sibling in ("audio__tts_provider", "audio__tts_model", "audio__tts_speed"):
        assert attrs[sibling]["left"] == voice["left"]
        assert attrs[sibling]["width"] == voice["width"]

    btn = attrs["audio__test_voice"]
    assert 'dlg:value="Test voice"' in xdl
    # Beside Voice, starting at or past the shared right edge — not on the next row.
    assert btn["top"] == voice["top"]
    voice_right = int(voice["left"]) + int(voice["width"])
    assert int(btn["left"]) >= voice_right
    window = ET.parse(xdl_path).getroot()
    dlg_width = int(window.get(f"{{{_DLG_NS}}}width") or 0)
    assert int(btn["left"]) + int(btn["width"]) <= dlg_width


def test_speech_tab_keep_replies_brief_checkbox(tmp_path: Path) -> None:
    """Short-answers checkbox is on the Speech page, with the other TTS controls."""
    xdl_path, xdl = _generate_settings_xdl(tmp_path)
    attrs = _control_attrs(xdl_path)

    box = attrs["audio__tts_short_answers"]
    assert 'dlg:value="Keep replies brief"' in xdl
    assert "Only applies while speech output (TTS) is on." in xdl
    assert re.search(
        r'dlg:id="audio__tts_short_answers"[^>]*dlg:checked="true"',
        xdl,
    )
    # Own row under the other speech checkboxes, above the provider select.
    # Not between Voice and Test voice (that pair shares a row via inline).
    assert int(attrs["audio__tts_sentence_mode"]["top"]) < int(box["top"]) < int(attrs["audio__tts_provider"]["top"])
    assert int(box["top"]) < int(attrs["audio__tts_voice"]["top"])


def test_starter_buttons_share_row_and_include_nvidia(tmp_path: Path) -> None:
    xdl_path, xdl = _generate_settings_xdl(tmp_path)
    attrs = _control_attrs(xdl_path)
    tops = {cid: vals["top"] for cid, vals in attrs.items() if vals["top"]}

    for btn_id in ("btn_openrouter", "btn_together", "btn_hf", "btn_nvidia"):
        assert btn_id in tops, f"{btn_id} missing from SettingsDialog.xdl"

    assert tops["btn_openrouter"] == tops["btn_together"] == tops["btn_hf"] == tops["btn_nvidia"]

    window = ET.parse(xdl_path).getroot()
    dlg_width = int(window.get(f"{{{_DLG_NS}}}width") or 0)
    assert dlg_width == 440

    label_right = int(attrs["label_get_api_key"]["left"]) + int(attrs["label_get_api_key"]["width"])
    btn_ids = ("btn_openrouter", "btn_together", "btn_hf", "btn_nvidia")
    prev_right = label_right
    for btn_id in btn_ids:
        assert int(attrs[btn_id]["height"]) == 16
        assert int(attrs[btn_id]["width"]) == 67
        btn_left = int(attrs[btn_id]["left"])
        assert btn_left >= prev_right
        assert btn_left + 67 <= dlg_width
        prev_right = btn_left + 67

    # Icons are placed directly on the buttons, no separate dlg:img elements
    for img_id in ("img_openrouter", "img_together", "img_huggingface", "img_nvidia"):
        assert img_id not in attrs
    assert "dlg:image-src=" not in xdl
    assert attrs["btn_ok"]["left"] == "170"


def _help_text_by_id(xdl_path: Path) -> dict[str, str | None]:
    """Map control dlg:id -> dlg:help-text (None when the attribute is absent)."""
    root = ET.parse(xdl_path).getroot()
    tips: dict[str, str | None] = {}
    for el in root.iter():
        ctrl_id = el.get(f"{{{_DLG_NS}}}id")
        if not ctrl_id or ctrl_id in tips:
            continue
        tips[ctrl_id] = el.get(f"{{{_DLG_NS}}}help-text")
    return tips


def _settings_keeps_caption_label(schema: dict) -> bool:
    """True when the generator leaves a FixedText caption next to the control."""
    if schema.get("inline_no_label"):
        return False
    widget = schema.get("widget", "text")
    if widget == "checkbox":
        return False
    if widget == "button" and not schema.get("show_button_label"):
        return False
    return True


def _iter_settings_field_modules(modules: list[dict]) -> list[dict]:
    """Tab modules plus config_inline children that land on those pages."""
    from plugin.chatbot.settings_tab_order import iter_settings_tab_modules

    tabs = list(iter_settings_tab_modules(modules))
    tab_names = {m["name"] for m in tabs}
    owners = list(tabs)
    seen = set(tab_names)
    for module in modules:
        target = module.get("config_inline")
        name = module.get("name")
        if isinstance(target, str) and target in tab_names and name not in seen:
            owners.append(module)
            seen.add(name)
    return owners


def test_settings_helpers_become_help_text(tmp_path: Path) -> None:
    """YAML helper (and tooltip: true) is dlg:help-text, not an hlp_* FixedText row."""
    from plugin._manifest import MODULES

    xdl_path, xdl = _generate_settings_xdl(tmp_path)
    tips = _help_text_by_id(xdl_path)

    assert "dlg:id=\"hlp_" not in xdl
    # Proven opt-in still works, and a helper without tooltip: true does too.
    assert tips["audio__tts_short_answers"] == (
        "Aim for about one paragraph unless the user asks for more. "
        "Only applies while speech output (TTS) is on."
    )
    assert tips["audio__tts_enabled"] == "Speak assistant responses aloud using text-to-speech."
    assert "label_audio__tts_enabled" not in tips
    assert tips["label_audio__stt_model"] == tips["audio__stt_model"]
    assert tips["audio__stt_model"] == "Speech-to-text model when the chat model cannot take audio input."
    assert tips["doc__grammar_proofreader_recheck"] == tips["label_doc__grammar_proofreader_recheck"]
    assert tips["doc__grammar_proofreader_recheck"].startswith("Clears cached grammar results")
    assert not tips.get("chatbot__max_tool_rounds")
    assert not tips.get("audio__test_voice")

    wired = 0
    for module in _iter_settings_field_modules(MODULES):
        prefix = str(module["name"]).replace(".", "_")
        config = module.get("config") or {}
        for field_name, schema in config.items():
            if not isinstance(schema, dict):
                continue
            if schema.get("internal") or schema.get("widget") in ("list_detail", "separator"):
                continue
            ctrl_id = f"{prefix}__{field_name}"
            expected = _settings_hover_text(schema)
            assert tips.get(ctrl_id) == (expected or None), ctrl_id
            label_id = f"label_{ctrl_id}"
            if _settings_keeps_caption_label(schema):
                assert label_id in tips, label_id
                assert tips.get(label_id) == (expected or None), label_id
            if expected:
                wired += 1
                assert f"hlp_{ctrl_id}" not in tips
    assert wired >= 30


def test_settings_explicit_tooltip_overrides_helper(tmp_path: Path) -> None:
    """A tooltip string wins; internal fields and hlp_* lines are not emitted."""
    modules = [
        {
            "name": "sample",
            "title": "Sample",
            "config": {
                "plain": {"widget": "text", "label": "Plain", "helper": "Helper tip"},
                "opt_in": {
                    "widget": "checkbox",
                    "label": "Opt",
                    "helper": "From helper",
                    "tooltip": True,
                },
                "override": {
                    "widget": "number",
                    "label": "Override",
                    "helper": "Helper not used",
                    "tooltip": "Explicit tip",
                },
                "markup": {
                    "widget": "password",
                    "label": "Markup",
                    "helper": 'Use A & B, say "hi" <there>',
                },
                "silent": {"widget": "button", "label": "Go", "button_text": "Go"},
                "hidden": {
                    "widget": "text",
                    "label": "Hidden",
                    "helper": "Should not appear",
                    "internal": True,
                },
            },
        }
    ]
    tpl = _REPO / "extension" / "Dialogs" / "SettingsDialog.xdl.tpl"
    out = tmp_path / "SettingsDialog-tips.xdl"
    generate_settings_dialog_tabs(modules, str(tpl), str(out))
    tips = _help_text_by_id(out)
    raw = out.read_text(encoding="utf-8")

    assert tips["sample__plain"] == "Helper tip"
    assert tips["label_sample__plain"] == "Helper tip"
    assert tips["sample__opt_in"] == "From helper"
    assert "label_sample__opt_in" not in tips
    assert tips["sample__override"] == "Explicit tip"
    assert tips["label_sample__override"] == "Explicit tip"
    assert tips["sample__markup"] == 'Use A & B, say "hi" <there>'
    assert "&amp;" in raw and "&lt;there&gt;" in raw
    assert not tips.get("sample__silent")
    assert "sample__hidden" not in tips
    assert "hlp_sample__plain" not in tips



# WriterAgent tests for SettingsDialog XDL layout generation
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path
import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from manifest_registry import _settings_hover_text, generate_settings_dialog_tabs  # noqa: E402

_DLG_NS = "http://openoffice.org/2000/dialog"


def _control_ids(xdl_path: Path) -> set[str]:
    """Return all dlg:id values from generated XDL."""
    root = ET.parse(xdl_path).getroot()
    return {
        ctrl_id
        for el in root.iter()
        if (ctrl_id := el.get(f"{{{_DLG_NS}}}id"))
    }


def _assert_controls_fit_and_do_not_overlap(root: ET.Element, *, check_overlap: bool = True) -> None:
    """Verify every child control fits inside the dialog and, when enabled, does not overlap."""
    dlg_width = int(root.get(f"{{{_DLG_NS}}}width") or 0)
    dlg_height = int(root.get(f"{{{_DLG_NS}}}height") or 0)
    assert dlg_width > 0 and dlg_height > 0

    by_page: dict[str, list[tuple[str, int, int, int, int]]] = {}
    for el in root.iter():
        # Skip <dlg:window> itself, which carries the dialog's own dimensions.
        if el is root:
            continue
        ctrl_id = el.get(f"{{{_DLG_NS}}}id")
        # __module__ is a 0x0 hidden metadata element carrying the module name.
        if not ctrl_id or ctrl_id == "__module__":
            continue
        page = el.get(f"{{{_DLG_NS}}}page") or "0"
        x = int(el.get(f"{{{_DLG_NS}}}left") or 0)
        y = int(el.get(f"{{{_DLG_NS}}}top") or 0)
        w = int(el.get(f"{{{_DLG_NS}}}width") or 0)
        h = int(el.get(f"{{{_DLG_NS}}}height") or 0)
        assert x >= 0 and y >= 0 and w > 0 and h > 0, f"Invalid geometry for {ctrl_id}"
        assert x + w <= dlg_width, f"{ctrl_id} overflows width: {x}+{w} > {dlg_width}"
        assert y + h <= dlg_height, f"{ctrl_id} overflows height: {y}+{h} > {dlg_height}"
        by_page.setdefault(page, []).append((ctrl_id, x, y, x + w, y + h))

    if not check_overlap:
        return

    shared = by_page.get("0", [])
    for page, boxes in sorted(by_page.items()):
        combined = boxes if page == "0" else shared + boxes
        for i, (c1, x1, y1, r1, b1) in enumerate(combined):
            for c2, x2, y2, r2, b2 in combined[i + 1 :]:
                overlaps = x1 < r2 and x2 < r1 and y1 < b2 and y2 < b1
                assert not overlaps, f"Page {page}: {c1} {(x1, y1, r1, b1)} overlaps {c2} {(x2, y2, r2, b2)}"


def _generate_settings_xdl(tmp_path: Path) -> tuple[Path, str]:
    from plugin._manifest import MODULES

    tpl = _REPO / "extension" / "Dialogs" / "SettingsDialog.xdl.tpl"
    out = tmp_path / "SettingsDialog.xdl"
    generate_settings_dialog_tabs(MODULES, str(tpl), str(out))
    assert out.is_file(), "generate_settings_dialog_tabs did not write output"
    return out, out.read_text(encoding="utf-8")


def test_settings_xdl_controls_within_bounds_and_non_overlapping(tmp_path: Path) -> None:
    """Overlapping controls hide captions or steal clicks, and controls past the dialog edge are clipped (e.g. helper lines wider than the window)."""
    xdl_path, _xdl = _generate_settings_xdl(tmp_path)
    _assert_controls_fit_and_do_not_overlap(ET.parse(xdl_path).getroot(), check_overlap=True)


def test_vision_standalone_controls_within_bounds() -> None:
    """Controls past the standalone dialog edge are clipped (e.g. helper lines wider than the Vision window)."""
    from manifest_xdl import generate_standalone_config_dialog
    from plugin._manifest import MODULES

    vision = next(module for module in MODULES if module["name"] == "vision")
    root = ET.fromstring(generate_standalone_config_dialog(vision))
    # Standalone label_* (height 14 at y+2) intentionally touches/overlaps hlp_* (y+15) by 1px;
    # keep width/height bounds checked so helper lines cannot overflow the window.
    _assert_controls_fit_and_do_not_overlap(root, check_overlap=False)


def test_settings_ok_writes_and_cancel_discards_roles(tmp_path: Path) -> None:
    """OK is default+ok and Cancel is cancel without default so Escape/Cancel returns false from execute() and nothing is written."""
    xdl_path, _xdl = _generate_settings_xdl(tmp_path)
    root = ET.parse(xdl_path).getroot()
    buttons = {
        el.get(f"{{{_DLG_NS}}}id"): el
        for el in root.findall(".//dlg:button", {"dlg": _DLG_NS})
    }
    ok_btn = buttons["btn_ok"]
    cancel_btn = buttons["btn_cancel"]
    assert ok_btn.get(f"{{{_DLG_NS}}}button-type") == "ok"
    assert ok_btn.get(f"{{{_DLG_NS}}}default") == "true"
    assert cancel_btn.get(f"{{{_DLG_NS}}}button-type") == "cancel"
    assert cancel_btn.get(f"{{{_DLG_NS}}}default") is None


def test_json_only_settings_absent_from_settings_xdl(tmp_path: Path) -> None:
    """Internal module.yaml keys are not emitted as Settings dialog controls."""
    xdl_path, _xdl = _generate_settings_xdl(tmp_path)
    ids = _control_ids(xdl_path)

    for hidden_id in (
        "mcp__cors_allow_private_origins",
        "scripting__native_run_script_modeless",
        "scripting__force_internal_script_editor",
        "chatbot__show_search_thinking",
        "chatbot__extend_selection_max_tokens",
        "chatbot__edit_selection_max_new_tokens",
    ):
        assert hidden_id not in ids

    assert "mcp__mcp_enabled" in ids
    assert "scripting__python_venv_path" in ids
    assert "scripting__xl_static_rewrite" in ids
    assert "scripting__python_geometric_recalc_order" in ids


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
    ids = _control_ids(out)

    assert "scripting__python_venv_path" in ids
    assert "scripting__ppt_master_data_path" not in ids
    assert "scripting__test_ppt_master_data" not in ids


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


def test_settings_helpers_become_help_text(tmp_path: Path) -> None:
    """YAML helper (and tooltip: true) is dlg:help-text, not an hlp_* FixedText row."""
    from plugin._manifest import MODULES
    from plugin.chatbot.settings_tab_order import iter_settings_field_modules

    xdl_path, xdl = _generate_settings_xdl(tmp_path)
    tips = _help_text_by_id(xdl_path)

    assert "dlg:id=\"hlp_" not in xdl

    wired = 0
    for module in iter_settings_field_modules(MODULES):
        prefix = str(module["name"]).replace(".", "_")
        config = module.get("config") or {}
        for field_name, schema in config.items():
            if not isinstance(schema, dict):
                continue
            if schema.get("internal") or schema.get("widget") == "separator":
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


@pytest.mark.parametrize("bad_widget", ["list_detail", "nope"])
def test_unknown_widget_rejected_by_settings_and_standalone(tmp_path: Path, bad_widget: str) -> None:
    """Settings and standalone generators reject unsupported or unknown widgets."""
    from manifest_xdl import generate_standalone_config_dialog

    fake_module = {
        "name": "sample",
        "title": "Sample",
        "config": {
            "bad_field": {"widget": bad_widget},
        },
    }
    tpl = _REPO / "extension" / "Dialogs" / "SettingsDialog.xdl.tpl"
    out = tmp_path / "SettingsDialog.xdl"
    with pytest.raises(ValueError, match="unknown widget"):
        generate_settings_dialog_tabs([fake_module], str(tpl), str(out))

    with pytest.raises(ValueError, match="unknown widget"):
        generate_standalone_config_dialog(fake_module)


def test_settings_dialog_saved_fields_agreement(tmp_path: Path) -> None:
    """Saved field specs for core, image, and modules match XDL control IDs."""
    from unittest.mock import MagicMock, patch
    from plugin._manifest import MODULES
    from plugin.chatbot.settings_dialog import get_settings_field_specs
    from plugin.chatbot.settings_fields import build_module_field_specs
    from plugin.chatbot.settings_tab_order import iter_settings_field_modules

    xdl_path, _ = _generate_settings_xdl(tmp_path)
    root = ET.parse(xdl_path).getroot()
    xdl_ids: set[str] = {
        ctrl_id
        for el in root.iter()
        if (ctrl_id := el.get(f"{{{_DLG_NS}}}id")) is not None
    }

    tab_modules = list(iter_settings_field_modules(MODULES))

    all_specs_names: set[str] = set()
    ctx = MagicMock()
    with (
        patch(
            "plugin.chatbot.settings_dialog.get_config",
            side_effect=lambda key: (
                "Square"
                if key == "image_default_aspect"
                else 512
                if key == "image_base_size"
                else 1.0
                if key == "temperature"
                else True
                if key in ("image_auto_gallery", "image_insert_frame")
                else 1
                if key.endswith("_tokens") or "timeout" in key
                else ""
            ),
        ),
        patch("plugin.chatbot.settings_dialog.get_text_model", return_value="dummy_text"),
        patch("plugin.chatbot.settings_dialog.get_image_model", return_value="dummy_image"),
        patch("plugin.chatbot.settings_dialog.get_current_endpoint", return_value="default"),
        patch("plugin.chatbot.settings_dialog.get_api_key_for_endpoint", return_value="secret"),
        patch("plugin.chatbot.settings_fields.get_config", return_value=""),
        patch("plugin.chatbot.settings_fields.call_options_provider", return_value=[{"label": "Stub", "value": "stub"}]),
    ):
        settings_specs = get_settings_field_specs(ctx)
        for spec in settings_specs:
            name = spec["name"]
            all_specs_names.add(name)
            assert name in xdl_ids, f"Field spec {name} missing from SettingsDialog.xdl"

        for m in tab_modules:
            specs = build_module_field_specs(m["name"], control_ids="prefixed")
            for spec in specs:
                name = spec["name"]
                all_specs_names.add(name)
                assert name in xdl_ids, f"Field spec {name} missing from SettingsDialog.xdl"

    for m in tab_modules:
        prefix = m["name"].replace(".", "_")
        cfg = m.get("config") or {}
        for fname, schema in cfg.items():
            if schema.get("settings_persist") is False and schema.get("widget") == "button":
                btn_id = f"{prefix}__{fname}"
                assert btn_id in xdl_ids, f"Button {btn_id} missing from SettingsDialog.xdl"
                assert btn_id not in all_specs_names, f"Button {btn_id} should not be in field specs"


def test_settings_dialog_tab_pages(tmp_path: Path) -> None:
    """Verify generated dlg:page numbers for template and module tab controls."""
    xdl_path, _ = _generate_settings_xdl(tmp_path)
    root = ET.parse(xdl_path).getroot()
    pages_by_id: dict[str, str] = {
        ctrl_id: page
        for el in root.iter()
        if (ctrl_id := el.get(f"{{{_DLG_NS}}}id")) is not None
        and (page := el.get(f"{{{_DLG_NS}}}page")) is not None
    }

    assert pages_by_id["endpoint"] == "1"
    assert pages_by_id["image_base_size"] == "2"
    assert pages_by_id["audio__stt_provider"] == "3"
    assert pages_by_id["doc__grammar_proofreader_enabled"] == "4"
    assert pages_by_id["chatbot__max_tool_rounds"] == "5"
    assert pages_by_id["embeddings__folder_search_mode"] == "6"
    assert pages_by_id["mcp__mcp_enabled"] == "7"
    assert pages_by_id["scripting__python_venv_path"] == "8"


def test_vision_standalone_dialog_fields_agreement() -> None:
    """Vision standalone field specs match controls in VisionSettingsDialog.xdl."""
    from unittest.mock import patch
    from manifest_xdl import generate_standalone_config_dialog
    from plugin._manifest import MODULES
    from plugin.chatbot.settings_fields import build_module_field_specs

    vision_module = next(m for m in MODULES if m["name"] == "vision")
    xml_str = generate_standalone_config_dialog(vision_module)
    root = ET.fromstring(xml_str)
    xdl_ids: set[str] = {
        ctrl_id
        for el in root.iter()
        if (ctrl_id := el.get(f"{{{_DLG_NS}}}id")) is not None
    }

    with patch("plugin.chatbot.settings_fields.get_config", return_value=""):
        specs = build_module_field_specs("vision", control_ids="flat")

    spec_names = {s["name"] for s in specs}
    for name in spec_names:
        assert name in xdl_ids, f"Vision spec {name} missing from VisionSettingsDialog.xdl"

    extras = xdl_ids - spec_names
    for extra in extras:
        is_known_extra = (
            extra in ("VisionSettingsDialog", "__module__", "btn_apply", "btn_ok", "btn_close")
            or extra.startswith("label_")
            or extra.startswith("hlp_")
            or extra.startswith("btn_tab_")
        )
        assert is_known_extra, f"Unexpected extra control {extra} in VisionSettingsDialog.xdl"


_EXPECTED_NUMERIC_BOUNDS: dict[str, tuple[int, int]] = {
    "doc__grammar_proofreader_batch_sentences": (1, 8),
    "doc__grammar_proofreader_max_in_flight": (1, 8),
    "calc__max_rows_display": (100, 100000),
    "chatbot__max_tool_rounds": (1, 200),
    "chatbot__web_cache_max_mb": (0, 500),
    "chatbot__web_cache_validity_days": (1, 30),
    "chatbot__audio_silence_stop_ms": (0, 15000),
    "mcp__mcp_port": (1024, 65535),
    "scripting__python_exec_timeout": (1, 600),
    "scripting__python_max_data_cells": (1000, 2000000),
}


def test_settings_numeric_fields_have_schema_bounds(tmp_path: Path) -> None:
    xdl_path, _xdl = _generate_settings_xdl(tmp_path)
    root = ET.parse(xdl_path).getroot()
    ns = {"dlg": _DLG_NS}
    elements = {el.get(f"{{{_DLG_NS}}}id"): el for el in root.findall(".//dlg:numericfield", ns)}

    for ctrl_id, (min_v, max_v) in _EXPECTED_NUMERIC_BOUNDS.items():
        assert ctrl_id in elements, f"Missing numeric field {ctrl_id}"
        el = elements[ctrl_id]
        assert el.get(f"{{{_DLG_NS}}}value-min") == str(min_v)
        assert el.get(f"{{{_DLG_NS}}}value-max") == str(max_v)
        assert el.get(f"{{{_DLG_NS}}}decimal-accuracy") == "0"
        assert el.get(f"{{{_DLG_NS}}}spin") == "true"
        assert el.get(f"{{{_DLG_NS}}}value-step") is None

        # Verify geometry attributes remain intact.
        assert int(el.get(f"{{{_DLG_NS}}}left") or 0) > 0
        assert int(el.get(f"{{{_DLG_NS}}}top") or 0) > 0
        assert int(el.get(f"{{{_DLG_NS}}}width") or 0) > 0
        assert int(el.get(f"{{{_DLG_NS}}}height") or 0) > 0


def test_settings_numeric_field_step_and_float_accuracy(tmp_path: Path) -> None:
    modules = [
        {
            "name": "doc",
            "title": "Document",
            "config": {
                "stepped_int": {
                    "type": "integer",
                    "widget": "number",
                    "label": "Stepped Int",
                    "min": 2,
                    "max": 10,
                    "step": 2,
                },
                "float_val": {
                    "type": "float",
                    "widget": "number",
                    "label": "Float Val",
                    "min": 0.5,
                    "max": 5.0,
                },
            },
        }
    ]
    tpl = _REPO / "extension" / "Dialogs" / "SettingsDialog.xdl.tpl"
    out = tmp_path / "SettingsDialog-synthetic.xdl"
    generate_settings_dialog_tabs(modules, str(tpl), str(out))
    root = ET.parse(out).getroot()
    ns = {"dlg": _DLG_NS}
    elements = {el.get(f"{{{_DLG_NS}}}id"): el for el in root.findall(".//dlg:numericfield", ns)}

    stepped = elements["doc__stepped_int"]
    assert stepped.get(f"{{{_DLG_NS}}}value-min") == "2"
    assert stepped.get(f"{{{_DLG_NS}}}value-max") == "10"
    assert stepped.get(f"{{{_DLG_NS}}}value-step") == "2"
    assert stepped.get(f"{{{_DLG_NS}}}decimal-accuracy") == "0"

    flt = elements["doc__float_val"]
    assert flt.get(f"{{{_DLG_NS}}}value-min") == "0.5"
    assert flt.get(f"{{{_DLG_NS}}}value-max") == "5.0"
    assert flt.get(f"{{{_DLG_NS}}}decimal-accuracy") == "1"
    assert flt.get(f"{{{_DLG_NS}}}value-step") is None


def test_standalone_numeric_field_step_and_bounds(tmp_path: Path) -> None:
    from manifest_xdl import generate_standalone_config_dialog
    from plugin._manifest import MODULES

    vision = next(module for module in MODULES if module["name"] == "vision")
    out = tmp_path / "VisionSettingsDialog.xdl"
    out.write_text(generate_standalone_config_dialog(vision), encoding="utf-8")
    root = ET.parse(out).getroot()
    ns = {"dlg": _DLG_NS}
    elements = {el.get(f"{{{_DLG_NS}}}id"): el for el in root.findall(".//dlg:numericfield", ns)}

    scale = elements["images_scale"]
    assert scale.get(f"{{{_DLG_NS}}}value-min") == "0.5"
    assert scale.get(f"{{{_DLG_NS}}}value-max") == "3.0"
    assert scale.get(f"{{{_DLG_NS}}}decimal-accuracy") == "1"
    assert scale.get(f"{{{_DLG_NS}}}value-step") is None

    synthetic = {
        "name": "custom",
        "title": "Custom",
        "config_dialog": {"id": "CustomDialog", "width": 400, "height": 300},
        "config": {
            "stepped_param": {
                "type": "integer",
                "widget": "number",
                "label": "Stepped Param",
                "min": 1,
                "max": 100,
                "step": 5,
            }
        },
    }
    synth_out = tmp_path / "CustomDialog.xdl"
    synth_out.write_text(generate_standalone_config_dialog(synthetic), encoding="utf-8")
    synth_root = ET.parse(synth_out).getroot()
    synth_elements = {el.get(f"{{{_DLG_NS}}}id"): el for el in synth_root.findall(".//dlg:numericfield", ns)}

    stepped = synth_elements["stepped_param"]
    assert stepped.get(f"{{{_DLG_NS}}}value-min") == "1"
    assert stepped.get(f"{{{_DLG_NS}}}value-max") == "100"
    assert stepped.get(f"{{{_DLG_NS}}}value-step") == "5"
    assert stepped.get(f"{{{_DLG_NS}}}decimal-accuracy") == "0"




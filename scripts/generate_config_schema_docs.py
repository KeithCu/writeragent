#!/usr/bin/env python3
"""Generate docs/writeragent-config-schema.md from module.yaml plus WriterAgentConfig.

Usage:
    python3 scripts/generate_config_schema_docs.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from manifest_common import write_if_changed  # noqa: E402

DEFAULT_OUTPUT = os.path.join(PROJECT_ROOT, "docs", "writeragent-config-schema.md")
SCHEMA_DOC_URL = (
    "https://github.com/KeithCu/writeragent/blob/master/docs/writeragent-config-schema.md"
)

_SKIP_WIDGETS = frozenset({"separator", "button", "label"})

# WriterAgentConfig fields that are secrets, UI memory, or unused — not for the schema doc.
_DATACLASS_OMIT = frozenset(
    {
        "api_keys_by_endpoint",
        "audio_support_map",
        "extension_update_check_epoch",
        "libreharper_update_check_epoch",
        "librepy_update_check_epoch",
        "last_latex_display_block",
        "last_latex_input",
        "last_python_script_name_calc",
        "last_python_script_name_draw",
        "last_python_script_name_writer",
        "model",  # legacy; ignored at read
        "saved_python_scripts",
        "_extra_config",
    }
)


def _fmt_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _md_cell(text: Any) -> str:
    s = "" if text is None else str(text)
    return s.replace("|", "\\|").replace("\n", " ")


def _option_summary(options: Any) -> str:
    if not isinstance(options, list) or not options:
        return ""
    parts: list[str] = []
    for opt in options:
        if isinstance(opt, dict):
            value = opt.get("value", "")
            label = opt.get("label", value)
            if label and label != value:
                parts.append(f"{value} ({label})")
            else:
                parts.append(str(value))
        else:
            parts.append(str(opt))
    return ", ".join(parts)


def _field_description(schema: dict[str, Any], field_name: str) -> str:
    bits: list[str] = []
    helper = schema.get("helper") or schema.get("help") or ""
    label = str(schema.get("label") or "")
    if helper:
        bits.append(str(helper))
    elif label and label != field_name:
        bits.append(label)
    opt = _option_summary(schema.get("options"))
    if opt:
        bits.append("Options: " + opt)
    text = " ".join(bits).strip()
    if schema.get("internal"):
        return f"Internal. {text}" if text else "Internal"
    return text


def _range_cell(schema: dict[str, Any]) -> str:
    has_min = "min" in schema
    has_max = "max" in schema
    if has_min and has_max:
        return f"`{_fmt_json(schema['min'])}`–`{_fmt_json(schema['max'])}`"
    if has_min:
        return f"≥ `{_fmt_json(schema['min'])}`"
    if has_max:
        return f"≤ `{_fmt_json(schema['max'])}`"
    return ""


_MISSING = object()


def _table_header() -> list[str]:
    return [
        "| Key | Type | Default | Range | Description |",
        "| --- | --- | --- | --- | --- |",
    ]


def _table_row(
    key: str,
    schema_type: Any,
    default: Any,
    range_text: str,
    description: str,
) -> str:
    type_cell = f"`{schema_type}`" if schema_type not in (None, "") else ""
    default_cell = f"`{_fmt_json(default)}`" if default is not _MISSING else ""
    return (
        f"| `{_md_cell(key)}` | {_md_cell(type_cell)} | {_md_cell(default_cell)} | "
        f"{_md_cell(range_text)} | {_md_cell(description)} |"
    )


def _should_skip_yaml_field(field_name: str, schema: dict[str, Any]) -> bool:
    if field_name.startswith("_"):
        return True
    widget = str(schema.get("widget") or "").lower()
    if widget in _SKIP_WIDGETS:
        return True
    if "type" not in schema and "default" not in schema:
        return True
    return False


def yaml_config_field_names(modules: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for module in modules:
        config = module.get("config")
        if isinstance(config, dict):
            names.update(str(k) for k in config)
    return names


def load_core_config_fields(
    *, skip_names: set[str] | None = None
) -> list[dict[str, Any]]:
    """Public WriterAgentConfig fields for the schema doc (type + default)."""
    import dataclasses

    from plugin.framework.config_schema import (
        WriterAgentConfig,
        _dataclass_field_default,
        _dataclass_field_type,
    )

    skip = _DATACLASS_OMIT | (skip_names or set())
    fields: list[dict[str, Any]] = []
    for f in dataclasses.fields(WriterAgentConfig):
        if f.name in skip or f.name.startswith("_") or f.name.startswith("last_"):
            continue
        default_val = _dataclass_field_default(f)
        schema_type = _dataclass_field_type(f) or "any"
        fields.append(
            {
                "name": f.name,
                "type": schema_type,
                "default": default_val,
            }
        )
    return fields


def _append_core_fields(lines: list[str], core_fields: list[dict[str, Any]]) -> None:
    if not core_fields:
        return
    lines.append("## Core (`WriterAgentConfig`)")
    lines.append("")
    lines.append("Top-level keys from the config dataclass (Settings dialog, chat, images).")
    lines.append("")
    lines.extend(_table_header())
    for field in core_fields:
        default = field["default"] if "default" in field else _MISSING
        lines.append(
            _table_row(
                field["name"],
                field.get("type"),
                default,
                "",
                str(field.get("description") or ""),
            )
        )
    lines.append("")


def render_config_schema_markdown(
    modules: list[dict[str, Any]],
    *,
    core_fields: list[dict[str, Any]] | None = None,
) -> str:
    """Build the schema markdown from module.yaml plus optional dataclass fields.

    ``core_fields=None`` loads ``WriterAgentConfig`` (omitting secrets / UI memory).
    Pass ``core_fields=[]`` to document yaml only.
    """
    if core_fields is None:
        core_fields = load_core_config_fields(skip_names=yaml_config_field_names(modules))

    lines: list[str] = [
        "<!-- Auto-generated by scripts/generate_config_schema_docs.py. Do not edit. -->",
        "",
        "# writeragent.json settings",
        "",
        "This is the full configuration schema for `writeragent.json`",
        "(LibreOffice user profile). **Only keys whose values differ from the",
        "defaults below are written to the file.** Omitted keys pick up the",
        "current default, so changing a default in a new WriterAgent release",
        "applies automatically unless you already overrode that key.",
        "",
        f"Canonical copy: [{SCHEMA_DOC_URL}]({SCHEMA_DOC_URL})",
        "",
        "`log_level` is `DEBUG` in a source checkout (`plugin/tests/` present)",
        "and `WARN` in a shipped OXT even if the yaml default is `DEBUG`.",
        "",
        "Internal keys are hidden from Settings but can still be set in JSON.",
        "",
    ]

    _append_core_fields(lines, core_fields)

    for module in modules:
        config = module.get("config")
        if not isinstance(config, dict) or not config:
            continue
        mod_name = str(module.get("name") or "")
        title = str(module.get("title") or mod_name)
        rows: list[str] = []
        for field_name, schema in config.items():
            if not isinstance(schema, dict) or _should_skip_yaml_field(field_name, schema):
                continue
            default = schema["default"] if "default" in schema else _MISSING
            rows.append(
                _table_row(
                    field_name,
                    schema.get("type"),
                    default,
                    _range_cell(schema),
                    _field_description(schema, field_name),
                )
            )
        if not rows:
            continue
        lines.append(f"## {title} (`{mod_name}`)")
        lines.append("")
        lines.extend(_table_header())
        lines.extend(rows)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def load_sorted_modules() -> list[dict[str, Any]]:
    from generate_manifest import find_modules, topo_sort

    try:
        import yaml
    except ImportError:
        print("ERROR: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
        sys.exit(1)

    plugin_dir = os.path.join(PROJECT_ROOT, "plugin")
    plugin_yaml_path = os.path.join(plugin_dir, "plugin.yaml")
    framework_manifest = None
    if os.path.isfile(plugin_yaml_path):
        with open(plugin_yaml_path) as f:
            framework_manifest = yaml.safe_load(f)
        if isinstance(framework_manifest, dict):
            framework_manifest.setdefault("name", "main")
        else:
            framework_manifest = None

    manifests = find_modules(plugin_dir)
    sorted_modules = topo_sort(manifests)
    if framework_manifest:
        sorted_modules.insert(0, framework_manifest)
    return sorted_modules


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate docs/writeragent-config-schema.md from module.yaml"
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="Markdown path (default: docs/writeragent-config-schema.md)",
    )
    args = parser.parse_args(argv)
    markdown = render_config_schema_markdown(load_sorted_modules())
    write_if_changed(args.output, markdown)
    print("  Wrote %s" % args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

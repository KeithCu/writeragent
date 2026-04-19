#!/usr/bin/env python3
"""Fetch Orca OpenRouter catalog, write slim JSON, refresh DEFAULT_MODELS openrouter fields."""

from __future__ import annotations

import argparse
import ast
import copy
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from plugin.framework.orca_catalog import (  # noqa: E402
    DEFAULT_ORCA_MODELS_URL,
    capability_mismatch_warning,
    context_length_mismatch_warning,
    filter_slim_catalog_tool_calling_only,
    merge_default_entry_from_slim,
    orca_slim_to_model_capability,
    slim_catalog_payload,
)
from plugin.framework.types import ModelCapability  # noqa: E402

DEFAULT_MODELS_PATH = os.path.join(PROJECT_ROOT, "plugin", "framework", "default_models.py")
# Generated slim catalog (not shipped in the .oxt; used for offline sync fallback + dev reference)
JSON_OUT = os.path.join(PROJECT_ROOT, "registry", "openrouter_models.json")

_CAPABILITY_ORDER = (
    "CHAT",
    "IMAGE",
    "EMBEDDINGS",
    "AUDIO",
    "MODERATIONS",
    "REALTIME",
    "CODE",
    "VISION",
    "TOOLS",
)


def _format_capability_expr(cap: ModelCapability | int) -> str:
    v = int(cap)
    if v == 0:
        return "ModelCapability.NONE"
    parts: list[str] = []
    for name in _CAPABILITY_ORDER:
        member = getattr(ModelCapability, name)
        iv = int(member)
        if v & iv:
            parts.append(f"ModelCapability.{name}")
            v &= ~iv
    if v:
        parts.append(f"ModelCapability({v})")
    return " | ".join(parts)


def _format_ids(ids: dict[str, Any]) -> str:
    keys = list(ids.keys())
    parts = ["{\n"]
    for i, k in enumerate(keys):
        comma = "," if i < len(keys) - 1 else ""
        parts.append(f'            "{k}": {json.dumps(ids[k])}{comma}\n')
    parts.append("        }")
    return "".join(parts)


def _format_model_dict(m: dict[str, Any]) -> str:
    lines: list[str] = ["    {"]
    order_first = ("display_name", "capability", "context_length", "ids")
    seen: set[str] = set()
    keys = [k for k in order_first if k in m]
    seen.update(keys)
    for k in m:
        if k not in seen:
            keys.append(k)
            seen.add(k)
    for i, k in enumerate(keys):
        val = m[k]
        comma = "," if i < len(keys) - 1 else ""
        if k == "capability":
            ce = _format_capability_expr(val)
            lines.append(f'        "capability": {ce}{comma}')
        elif k == "ids" and isinstance(val, dict):
            id_s = _format_ids(val)
            lines.append(f'        "ids": {id_s}{comma}')
        elif k == "context_length":
            lines.append(f'        "context_length": {int(val)}{comma}')
        elif isinstance(val, bool):
            lines.append(f'        "{k}": {str(val)}{comma}')
        else:
            lines.append(f'        "{k}": {json.dumps(val, ensure_ascii=False)}{comma}')
    lines.append("    }")
    return "\n".join(lines)


def format_default_models_list(models: list[dict[str, Any]]) -> str:
    """Return source for the list expression only (starts with `[`, ends with `]`)."""
    parts = ["["]
    for i, m in enumerate(models):
        comma = "," if i < len(models) - 1 else ""
        parts.append(_format_model_dict(m) + comma)
    parts.append("]")
    return "\n".join(parts)


def _line_start_offsets(source: str) -> list[int]:
    offsets = [0]
    for i, c in enumerate(source):
        if c == "\n":
            offsets.append(i + 1)
    return offsets


def _node_char_span(source: str, node: ast.AST) -> tuple[int, int]:
    lo = getattr(node, "lineno", None)
    co = getattr(node, "col_offset", None)
    el = getattr(node, "end_lineno", None)
    ec = getattr(node, "end_col_offset", None)
    if None in (lo, co, el, ec):
        raise ValueError("AST node missing position info (need Python 3.8+)")
    offs = _line_start_offsets(source)
    start = offs[lo - 1] + co
    end = offs[el - 1] + ec
    return start, end


def fetch_json(url: str, timeout: float = 60.0) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "WriterAgent-orca-sync/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object from Orca API")
    return data


def load_json_path(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")
    return data


def build_slim_by_id(slim_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    models = slim_payload.get("models")
    if not isinstance(models, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for m in models:
        if isinstance(m, dict) and m.get("id"):
            out[str(m["id"])] = m
    return out


def merge_defaults_for_openrouter(
    defaults: list[dict[str, Any]],
    slim_by_id: dict[str, dict[str, Any]],
    *,
    strict: bool,
) -> list[dict[str, Any]]:
    merged = copy.deepcopy(defaults)
    for entry in merged:
        ids = entry.get("ids")
        if not isinstance(ids, dict):
            continue
        oid = ids.get("openrouter")
        if not oid:
            continue
        slim = slim_by_id.get(str(oid))
        if slim is None:
            msg = (
                f"No Orca entry for openrouter id {oid!r} "
                f"({entry.get('display_name', '?')})"
            )
            if strict:
                raise SystemExit(msg)
            print(f"Warning: {msg}", file=sys.stderr)
            continue
        label = str(entry.get("display_name", "?"))
        orca_caps_int = int(orca_slim_to_model_capability(slim))
        existing_caps = int(entry.get("capability", ModelCapability.NONE))
        cap_w = capability_mismatch_warning(str(oid), label, existing_caps, orca_caps_int)
        if cap_w:
            print(f"Warning: {cap_w}", file=sys.stderr)
        cl_w = context_length_mismatch_warning(
            str(oid),
            label,
            entry.get("context_length"),
            slim.get("context_length"),
        )
        if cl_w:
            print(f"Warning: {cl_w}", file=sys.stderr)
        merge_default_entry_from_slim(entry, slim)
    return merged


def replace_default_models_list(source: str, new_list_src: str) -> str:
    tree = ast.parse(source)
    target = None
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "DEFAULT_MODELS":
                target = node
                break
    if target is None or target.value is None:
        raise ValueError("Could not find DEFAULT_MODELS annotated assignment")
    start, end = _node_char_span(source, target.value)
    return source[:start] + new_list_src + source[end:]


def _payload_from_disk_or_raw(data: dict[str, Any], source_url: str) -> dict[str, Any]:
    """If disk file is already slim, use as-is; else normalize raw Orca API shape."""
    models = data.get("models")
    if isinstance(models, list) and models and isinstance(models[0], dict):
        if "providers" not in models[0]:
            return data
    return slim_catalog_payload(data, source_url=source_url)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--url",
        default=os.environ.get("ORCA_MODELS_URL", DEFAULT_ORCA_MODELS_URL),
        help="Orca API URL (default ORCA_MODELS_URL or built-in)",
    )
    ap.add_argument(
        "--use-cached",
        action="store_true",
        help="Read JSON from --cache-file instead of fetching",
    )
    ap.add_argument(
        "--cache-file",
        default=JSON_OUT,
        help="Path for --use-cached input (default: registry/openrouter_models.json)",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="Exit with error if any curated openrouter id is missing from Orca",
    )
    ap.add_argument(
        "--json-out",
        default=JSON_OUT,
        help="Output path for slim catalog JSON",
    )
    ap.add_argument(
        "--default-models",
        default=DEFAULT_MODELS_PATH,
        help="Path to default_models.py",
    )
    args = ap.parse_args()

    slim_payload: dict[str, Any]
    if args.use_cached:
        disk = load_json_path(args.cache_file)
        slim_payload = _payload_from_disk_or_raw(disk, args.url)
    else:
        try:
            raw_api = fetch_json(args.url)
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
            print(f"Fetch failed ({args.url}): {e}", file=sys.stderr)
            if not os.path.isfile(args.cache_file):
                return 1
            print(f"Falling back to cached file {args.cache_file}", file=sys.stderr)
            disk = load_json_path(args.cache_file)
            slim_payload = _payload_from_disk_or_raw(disk, args.url)
        else:
            slim_payload = slim_catalog_payload(raw_api, source_url=args.url)

    # Cached slim files may predate tool-only filtering; always enforce.
    slim_payload = filter_slim_catalog_tool_calling_only(slim_payload)

    slim_by_id = build_slim_by_id(slim_payload)

    os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
    with open(args.json_out, "w", encoding="utf-8") as f:
        json.dump(slim_payload, f, ensure_ascii=False, indent=2)
        f.write("\n")

    from plugin.framework.default_models import DEFAULT_MODELS

    merged = merge_defaults_for_openrouter(
        list(DEFAULT_MODELS),
        slim_by_id,
        strict=args.strict,
    )

    with open(args.default_models, encoding="utf-8") as f:
        src = f.read()
    new_list = format_default_models_list(merged)
    out = replace_default_models_list(src, new_list)
    with open(args.default_models, "w", encoding="utf-8", newline="\n") as f:
        f.write(out)

    print(f"Wrote {args.json_out} and updated {args.default_models}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

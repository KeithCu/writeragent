# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Named prompt/tool-text slices for offline MIPROv2 (not the whole ambient prompt).

MIPROv2 proposes replacements for one short blob. ``apply_named_slice``
injects that blob into the same system prompt / OpenAI schemas
``run_eval_multi`` / ``llm_chat_eval`` already build. Production files
are not written.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from eval_prompts import (
    get_eval_system_prompt,
    replace_prompt_slice,
)

SliceKind = Literal["prompt_fragment", "tool_description", "tool_param", "full_prompt"]


@dataclass(frozen=True)
class SliceSpec:
    """One injectable fragment MIPROv2 is allowed to rewrite."""

    name: str
    kind: SliceKind
    # prompt_fragment: attribute on plugin.framework.prompts (e.g. CALC_CORE_DIRECTIVES)
    prompt_attr: str = ""
    # tool_description / tool_param
    tool_name: str = ""
    param_name: str = ""


# Short production blobs Keith asked to optimize — not DEFAULT_* templates.
SLICE_SPECS: dict[str, SliceSpec] = {
    "calc_core": SliceSpec("calc_core", "prompt_fragment", prompt_attr="CALC_CORE_DIRECTIVES"),
    "writer_core": SliceSpec("writer_core", "prompt_fragment", prompt_attr="WRITER_CORE_DIRECTIVES"),
    # apply_document_content / HTML-diff contract (Writer ambient prompt; recency block).
    "apply_html": SliceSpec(
        "apply_html", "prompt_fragment", prompt_attr="WRITER_APPLY_DOCUMENT_HTML_RULES"
    ),
    "write_formula_range": SliceSpec(
        "write_formula_range", "tool_description", tool_name="write_formula_range"
    ),
    "write_formula_range.values": SliceSpec(
        "write_formula_range.values",
        "tool_param",
        tool_name="write_formula_range",
        param_name="values",
    ),
    "sort_range": SliceSpec("sort_range", "tool_description", tool_name="sort_range"),
    # Fallback: treat the assembled eval prompt as the instruction (opaque).
    "full_prompt": SliceSpec("full_prompt", "full_prompt"),
}

DEFAULT_LLM_SLICE = "calc_core"


def list_slice_names() -> list[str]:
    return list(SLICE_SPECS)


def get_slice_spec(name: str) -> SliceSpec:
    key = (name or "").strip()
    if key not in SLICE_SPECS:
        known = ", ".join(list_slice_names())
        raise ValueError(f"Unknown slice {name!r}. Choose one of: {known}")
    return SLICE_SPECS[key]


def _prompt_constant(attr: str) -> str:
    from plugin.framework import prompts as pr

    text = getattr(pr, attr, None)
    if not isinstance(text, str) or not text:
        raise ValueError(f"plugin.framework.prompts.{attr} is not a non-empty string")
    return text


def _tool_baseline(tool_name: str, param_name: str = "") -> str:
    from plugin.calc.cells import SortRange, WriteCellRange

    cls: Any
    if tool_name == "write_formula_range":
        cls = WriteCellRange
    elif tool_name == "sort_range":
        cls = SortRange
    else:
        raise ValueError(f"No baseline class for tool {tool_name!r}")
    if not param_name:
        return str(cls.description)
    props = (cls.parameters or {}).get("properties") or {}
    param = props.get(param_name) or {}
    desc = param.get("description")
    if not isinstance(desc, str) or not desc:
        raise ValueError(f"{tool_name}.{param_name} has no description")
    return desc


def get_slice_baseline(name: str, *, prompt_task_id: str = "") -> str:
    """Current production text for this slice (MIPROv2 seed / length baseline)."""
    spec = get_slice_spec(name)
    if spec.kind == "prompt_fragment":
        return _prompt_constant(spec.prompt_attr)
    if spec.kind == "tool_description":
        return _tool_baseline(spec.tool_name)
    if spec.kind == "tool_param":
        return _tool_baseline(spec.tool_name, spec.param_name)
    return get_eval_system_prompt(prompt_task_id)


def schema_patches_for_slice(name: str, slice_text: str) -> dict[str, dict[str, Any]] | None:
    """Patches for ``apply_schema_patches``, or None when the slice is prompt-only."""
    spec = get_slice_spec(name)
    if spec.kind == "tool_description":
        return {spec.tool_name: {"description": slice_text}}
    if spec.kind == "tool_param":
        return {spec.tool_name: {"parameters": {spec.param_name: slice_text}}}
    return None


def apply_named_slice(
    task_id: str,
    slice_name: str,
    slice_text: str,
    *,
    prompt_task_id: str = "",
) -> tuple[str, dict[str, dict[str, Any]] | None]:
    """Build the eval system prompt + optional schema patches for one trial.

    Prompt assembly stays ``get_eval_system_prompt`` (same as run_eval_multi).
    Tool slices leave the prompt alone and only rewrite advertised schemas.
    """
    spec = get_slice_spec(slice_name)
    if spec.kind == "full_prompt":
        return slice_text, None
    prompt = get_eval_system_prompt(task_id)
    if spec.kind == "prompt_fragment":
        original = get_slice_baseline(slice_name, prompt_task_id=prompt_task_id)
        return replace_prompt_slice(prompt, original, slice_text), None
    return prompt, schema_patches_for_slice(slice_name, slice_text)

#!/usr/bin/env python3
# WriterAgent - eval-2 / python domain → shapes American flag oracle
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Soft structural scorer for the eval-2 python-shapes flag.

The Ask is one line: use the python domain to make an American flag using
shapes. Pass/fail is the saved Writer document plus the run-stamp
``writeragent_debug.log``. Chat Ready / STREAM_DONE is never consulted.

Process evidence is taken from executed tool calls (``Tool call:``,
``streaming_loop: accumulated tool_calls``, ``SmolToolAdapter executing``,
``tool-async-``, and ``=== Sync response:`` ``tool_calls``). Few-shot
text that merely mentions ``delegate_tool_domains`` or ``wa.shape.upsert``
does not count — those strings are in the python shapes prompt and show
up in logged request ``"content"`` bodies. A nested ``delegate_tool_domains``
often has empty args on the Smol / tool-async line; that still counts as
shapes when an executed payload shows ``domains`` including shapes, or
when no executed payload names another domain and a ``run_venv_python_script``
after that call places shapes (``wa.shape.upsert``). A footnotes-only
delegation does not.

Geometry is the Writer draw page inside the ``.odt``. LibreOffice writes
``svg:width`` / ``svg:height`` in inches on a Writer save (19001 HMM →
``7.4807in``); cm / mm / pt are accepted too. Page scale is the maximum
shape width in HMM (1/100 mm). A full-page flag is about 10000–20000 HMM
wide. Width near 1900 HMM is a ~19 mm speck.

Hard pass is the matrix floor, not a perfect canton: python specialized
path, ``delegate_tool_domains`` (shapes), ``run_venv_python_script``, and
a page-scale composite of stripe-like rects. gpt-oss-20b already gets
that far on this Ask — the flag is recognizable and the stars are
imperfect. That almost-flag is a good outcome. Star count and placement
are soft: messy or under-counted stars do not fail. Wrong route
(images / PNG), no venv, a ~1900 HMM speck, or a blank page do fail.

``shape_group`` / ``draw:g`` is a bonus. Writer grouping hits a known UNO
``ShapeCollection`` bug (#927), so a loose set of shapes still passes.

A preview PNG is the page thumbnail LibreOffice already stored in the
saved ``.odt`` (``Thumbnails/thumbnail.png``). ``--score`` copies it to
``preview.png`` beside that file so each model entry has a picture.
A missing thumbnail does not add a failure. Outer tool-loop rounds from
the debug log (``Tool-calling loop START`` / ``Tool loop round N``) are
recorded on the result and do not change pass/fail.

Usage:
  .venv/bin/python scripts/eval_2_python_shapes_flag_oracle.py path/to/final_flag.odt
  .venv/bin/python scripts/eval_2_python_shapes_flag_oracle.py path/to/runs/<stamp>/
  .venv/bin/python scripts/eval_2_headed.py --task python-shapes-flag --score path/to/runs/<stamp>/
"""
from __future__ import annotations

import argparse
import json
import re
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from eval_2_debug_log import DEBUG_LOG_FILENAME
from eval_2_headed import PYTHON_SHAPES_ODT_NAME, PYTHON_SHAPES_STAMP_ODT

_DRAW_NS = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
_SVG_NS = "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
_DRAW_WIDTH = f"{{{_SVG_NS}}}width"
_DRAW_HEIGHT = f"{{{_SVG_NS}}}height"
_DRAW_TYPE = f"{{{_DRAW_NS}}}type"
_DRAW_NAME = f"{{{_DRAW_NS}}}name"
_DRAW_POINTS = f"{{{_DRAW_NS}}}points"

# Drawing pieces that can be stripes or stars. Frames / lines / connectors
# are text boxes and rules, not flag geometry.
_SHAPE_LOCALS = frozenset({
    "rect",
    "custom-shape",
    "ellipse",
    "circle",
    "polygon",
    "regular-polygon",
})
_RECT_TYPES = frozenset({"rectangle", "rect", "round-rectangle", "roundrect"})

# Hard geometry: a page-scale striped field. An earlier headed pass was
# ~14–15 rects at max width ≈ 19001 HMM. Exact 13 stripes is not required.
# gpt-oss-20b's almost-flag (recognizable, imperfect stars) is the matrix
# floor — do not demand a perfect 50-star canton for pass.
MIN_STRIPE_RECTS = 6
MIN_MAX_WIDTH_HMM = 10_000
STRIPE_MIN_ASPECT = 3.0
# Soft only. Below this, the result still passes when the hard gate is
# met, and partial_score trims one soft check. Not a fail.
SOFT_STAR_FIELD = 8
# A canton block is a non-stripe rect large enough to be the union, not a star.
_CANTON_MIN_HMM = 2_000
# A 5-point star polygon has 10 vertices. A rectangle polygon has 4.
_MIN_STAR_POLYGON_POINTS = 10

# Hard checks only. Star shortfall is the extra soft check in partial_score.
CHECK_COUNT = 7
SOFT_CHECK_COUNT = 1

_ODF_THUMBNAIL = "Thumbnails/thumbnail.png"
PREVIEW_PNG_NAME = "preview.png"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_PREFERRED_ODT = (
    PYTHON_SHAPES_STAMP_ODT,
    PYTHON_SHAPES_ODT_NAME,
)

_LENGTH_RE = re.compile(
    r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(cm|mm|in|pt|pc|px)?$",
    re.IGNORECASE,
)
# HMM is 1/100 mm. Unitless values are already HMM (UNO Size). Writer
# saves usually carry a unit; inches are what storeToURL wrote in probe.
_HMM_PER_UNIT = {
    "mm": 100.0,
    "cm": 1000.0,
    "in": 2540.0,
    "pt": 2540.0 / 72.0,
    "pc": 2540.0 / 6.0,
    "px": 2540.0 / 96.0,
    "": 1.0,
}

_LOOP_START_RE = re.compile(r"Tool-calling loop START \(max (\d+) rounds\)")
# plugin/chatbot/tool_loop.py log.debug — 0-based outer chat rounds.
_LOOP_ROUND_RE = re.compile(r"Tool loop round (\d+):")
_TOOL_CALL_RE = re.compile(r"Tool call:\s*([A-Za-z_][\w]*)\((.*)\)\s*$")
_ACCUM_CALL_RE = re.compile(
    r'"name"\s*:\s*"([^"]+)"\s*,\s*"arguments"\s*:\s*"((?:\\.|[^"\\])*)"',
)
_SMOL_EXEC_RE = re.compile(
    r"SmolToolAdapter executing (?:async|sync) tool '([^']+)'",
)
_TOOL_ASYNC_RE = re.compile(r"tool-async-([A-Za-z_][\w]*)")
_SYNC_RESPONSE_MARK = "=== Sync response:"
# Request few-shot lives on logged ``"content"`` lines. Reasoning is the
# model's prose, not the call. An arguments line in a sync response does
# not carry those keys (indent=2 puts them on their own lines). Skipping
# them keeps the shapes example from counting.
_PROSE_KEY_RE = re.compile(
    r"""["'](?:content|reasoning|reasoning_details|refusal)["']\s*:""",
)
_SHAPES_DOMAIN_RE = re.compile(
    r"""["']domains["']\s*:\s*\[[^\]]*["']shapes["']""",
    re.IGNORECASE,
)
_DOMAINS_LIST_RE = re.compile(
    r"""["']domains["']\s*:\s*\[[^\]]*\]""",
    re.IGNORECASE,
)
_SYNC_SKIP_KEYS = frozenset({
    "content",
    "reasoning",
    "reasoning_details",
    "refusal",
})
_PYTHON_DOMAIN_RE = re.compile(
    r"""["']domain["']\s*:\s*["']python["']""",
    re.IGNORECASE,
)
_IMAGES_DOMAIN_RE = re.compile(
    r"""["']domain["']\s*:\s*["']images["']""",
    re.IGNORECASE,
)
_STAR_TYPE_RE = re.compile(r"star[-_]?\d*", re.IGNORECASE)
_IMAGE_TOOL_NAMES = frozenset({
    "image_generate",
    "image_insert",
    "image_download",
    "image_replace",
})
_CREATE_SHAPE_MARKERS = (
    "create_shape snapshot",
    "create_shape safe_create",
    "shape_upsert (create) branch",
)


@dataclass
class OracleResult:
    """Hard gate plus a soft star note.

    ``passed`` is true only when ``failures`` is empty. Star shortfall lives
    in ``soft`` and does not flip ``passed`` (20b almost-flag still passes).
    ``partial_score`` is ``1 - (failures + soft) / (checks + 1 soft check)``.
    Grouping and script-level ``wa.shape.upsert`` are bonuses, not checks.
    """

    passed: bool
    failures: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)
    checks: int = CHECK_COUNT
    partial_score: float = 0.0
    bonuses: list[str] = field(default_factory=list)
    shape_count: int = 0
    stripe_rects: int = 0
    star_shapes: int = 0
    canton_rects: int = 0
    max_width_hmm: int = 0
    grouped: bool = False
    python_domain: bool = False
    delegate_shapes: bool = False
    run_venv: bool = False
    llm_shape_upsert: bool = False
    images_path: bool = False
    script_placement: bool = False
    # Outer chat loop only. None when the log has no such lines. Not a check.
    tool_rounds_used: int | None = None
    tool_rounds_budget: int | None = None
    preview_png: str | None = None

    def to_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class FlagGeometry:
    """Shape counts from a saved Writer ``.odt`` (zip XML, not live UNO)."""

    shape_count: int
    stripe_rects: int
    star_shapes: int
    max_width_hmm: int
    grouped: bool
    canton_rects: int = 0


@dataclass(frozen=True)
class PathEvidence:
    """Executed-call evidence. Prompt few-shot text is not evidence."""

    python_domain: bool
    delegate_shapes: bool
    run_venv: bool
    llm_shape_upsert: bool
    images_path: bool
    script_placement: bool
    shape_group_call: bool


@dataclass(frozen=True)
class _LoggedCall:
    """One executed tool call found in a debug log.

    ``pos`` is a character offset. ``from_sync`` is a ``=== Sync response:``
    ``tool_calls`` entry. The worker logs that JSON before the main thread
    flushes ``SmolToolAdapter``, so those args can sit above the Smol line
    in the file and are still the call that ran.
    """

    name: str
    args: str
    pos: int
    from_sync: bool


def length_to_hmm(raw: str) -> int | None:
    """Convert an ODF length (``7.4807in``, ``19.001cm``, …) to HMM.

    Nearest HMM so LibreOffice's 4-decimal inch export of 19001 HMM
    (``7.4807in``) rounds back to 19001.
    """
    text = (raw or "").strip()
    if not text:
        return None
    match = _LENGTH_RE.match(text)
    if match is None:
        return None
    unit = (match.group(2) or "").lower()
    factor = _HMM_PER_UNIT.get(unit)
    if factor is None:
        return None
    return int(round(float(match.group(1)) * factor))


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _unescape_args(args: str) -> str:
    """Turn logged JSON-string escapes back into quotes for domain matching."""
    return args.replace('\\"', '"').replace("\\'", "'")


def _polygon_points(node: ET.Element) -> int:
    raw = node.get(_DRAW_POINTS) or ""
    return len(raw.split())


def _geometry_type(node: ET.Element) -> str:
    for child in node:
        if _local(child.tag) == "enhanced-geometry":
            return (child.get(_DRAW_TYPE) or "").strip()
    return (node.get(_DRAW_TYPE) or "").strip()


def _is_rect(local: str, geom_type: str) -> bool:
    if local == "rect":
        return True
    return geom_type.lower().replace("_", "-") in _RECT_TYPES


def _is_star(local: str, geom_type: str, name: str, points: int) -> bool:
    blob = f"{geom_type} {name}"
    if _STAR_TYPE_RE.search(blob):
        return True
    if local in {"polygon", "regular-polygon"} and points >= _MIN_STAR_POLYGON_POINTS:
        return True
    return False


def _is_stripe(width_hmm: int | None, height_hmm: int | None) -> bool:
    if width_hmm is None or height_hmm is None or height_hmm <= 0 or width_hmm <= 0:
        return False
    return (width_hmm / height_hmm) >= STRIPE_MIN_ASPECT


def _is_canton(width_hmm: int | None, height_hmm: int | None) -> bool:
    """Non-stripe rect big enough to be a union block, not a star speck."""
    if width_hmm is None or height_hmm is None:
        return False
    return width_hmm >= _CANTON_MIN_HMM and height_hmm >= _CANTON_MIN_HMM


def read_writer_flag(path: Path) -> FlagGeometry:
    """Count stripe-like rects, star-like shapes, and max width from ``content.xml``."""
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("content.xml"))
    shape_count = 0
    stripe_rects = 0
    star_shapes = 0
    canton_rects = 0
    max_width = 0
    grouped = False
    for node in root.iter():
        local = _local(node.tag)
        width = length_to_hmm(node.get(_DRAW_WIDTH) or "")
        if width is not None and width > max_width:
            max_width = width
        if local == "g":
            grouped = True
            continue
        if local not in _SHAPE_LOCALS:
            continue
        shape_count += 1
        geom = _geometry_type(node)
        name = node.get(_DRAW_NAME) or ""
        height = length_to_hmm(node.get(_DRAW_HEIGHT) or "")
        if _is_rect(local, geom):
            if _is_stripe(width, height):
                stripe_rects += 1
            elif _is_canton(width, height):
                canton_rects += 1
            continue
        if _is_star(local, geom, name, _polygon_points(node)):
            star_shapes += 1
    return FlagGeometry(
        shape_count=shape_count,
        stripe_rects=stripe_rects,
        star_shapes=star_shapes,
        max_width_hmm=max_width,
        grouped=grouped,
        canton_rects=canton_rects,
    )


def _extract_json_object(text: str, start: int) -> tuple[str, int] | None:
    """Brace-match one JSON object at or after ``start``, respecting strings."""
    begin = text.find("{", start)
    if begin < 0:
        return None
    depth = 0
    in_str = False
    escaped = False
    for index in range(begin, len(text)):
        ch = text[index]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[begin:index + 1], begin
    return None


def _append_sync_tool_calls(node: object, out: list[_LoggedCall], pos: int) -> None:
    """Collect ``tool_calls`` objects. Skip message content and reasoning.

    Those fields are where a sync response quotes prose. The executed call
    is the ``function`` name plus arguments. Request-body few-shot is not
    inside this object; it is logged on the earlier ``Messages`` record.
    """
    if isinstance(node, dict):
        raw_calls = node.get("tool_calls")
        if isinstance(raw_calls, list):
            for item in raw_calls:
                if not isinstance(item, dict):
                    continue
                fn = item.get("function")
                if not isinstance(fn, dict):
                    continue
                name = fn.get("name")
                if not isinstance(name, str) or not name:
                    continue
                raw_args = fn.get("arguments")
                if isinstance(raw_args, str):
                    arg_text = raw_args
                elif raw_args is None:
                    arg_text = ""
                else:
                    arg_text = json.dumps(raw_args, ensure_ascii=False)
                out.append(_LoggedCall(name, arg_text, pos, True))
        for key, val in node.items():
            if not isinstance(key, str) or key in _SYNC_SKIP_KEYS or key == "tool_calls":
                continue
            _append_sync_tool_calls(val, out, pos)
    elif isinstance(node, list):
        for item in node:
            _append_sync_tool_calls(item, out, pos)


def _sync_response_calls(log_text: str) -> list[_LoggedCall]:
    """Tool calls the nested smol model actually emitted (sync path).

    Specialized agents use ``request_with_tools`` with ``stream=False``.
    That logs ``=== Sync response:`` plus the assistant ``tool_calls``,
    then ``SmolToolAdapter`` runs the tool and logs only the name.
    """
    calls: list[_LoggedCall] = []
    start = 0
    while True:
        idx = log_text.find(_SYNC_RESPONSE_MARK, start)
        if idx < 0:
            break
        extracted = _extract_json_object(log_text, idx + len(_SYNC_RESPONSE_MARK))
        if extracted is None:
            start = idx + len(_SYNC_RESPONSE_MARK)
            continue
        payload, payload_at = extracted
        start = payload_at + len(payload)
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        _append_sync_tool_calls(data, calls, payload_at)
    return calls


def _calls_from_log(log_text: str) -> list[_LoggedCall]:
    """Tool name + raw arguments from lines that record an executed call.

    Request-body few-shot examples live in ``"content"`` lines and are
    skipped. ``accumulated tool_calls`` is the model actually emitting a
    call; ``Tool call:`` / ``SmolToolAdapter`` / ``tool-async-`` are the
    host running it. Sync-response ``tool_calls`` supply the arguments the
    Smol line does not include.
    """
    calls: list[_LoggedCall] = []
    offset = 0
    for line in log_text.splitlines(keepends=True):
        tool_match = _TOOL_CALL_RE.search(line)
        if tool_match is not None:
            calls.append(_LoggedCall(tool_match.group(1), tool_match.group(2), offset + tool_match.start(), False))
        if "accumulated tool_calls" in line:
            for name, args in _ACCUM_CALL_RE.findall(line):
                calls.append(_LoggedCall(name, args, offset, False))
        smol_match = _SMOL_EXEC_RE.search(line)
        if smol_match is not None:
            calls.append(_LoggedCall(smol_match.group(1), "", offset + smol_match.start(), False))
        if "tool-async-" in line:
            for name in _TOOL_ASYNC_RE.findall(line):
                at = line.find(f"tool-async-{name}")
                calls.append(_LoggedCall(name, "", offset + max(at, 0), False))
        offset += len(line)
    calls.extend(_sync_response_calls(log_text))
    return calls


def _dtd_exec_pos(log_text: str) -> int | None:
    """First Smol / tool-async execution of ``delegate_tool_domains``."""
    pos: int | None = None
    for match in _SMOL_EXEC_RE.finditer(log_text):
        if match.group(1) == "delegate_tool_domains":
            pos = match.start()
            break
    for match in _TOOL_ASYNC_RE.finditer(log_text):
        if match.group(1) != "delegate_tool_domains":
            continue
        if pos is None or match.start() < pos:
            pos = match.start()
    return pos


def _is_run_venv_exec_line(line: str) -> bool:
    smol_match = _SMOL_EXEC_RE.search(line)
    if smol_match is not None and smol_match.group(1) == "run_venv_python_script":
        return True
    if "tool-async-run_venv_python_script" in line:
        return True
    tool_match = _TOOL_CALL_RE.search(line)
    if tool_match is not None and tool_match.group(1) == "run_venv_python_script":
        return True
    return "accumulated tool_calls" in line and "run_venv_python_script" in line


def _line_carries_call_payload(line: str) -> bool:
    """True for a logged call body, not prose that happens to mention domains."""
    if "Tool call:" in line or "accumulated tool_calls" in line or "Calling tool:" in line:
        return True
    return '"arguments"' in line or "'arguments'" in line


def _domain_markers_outside_content(log_text: str) -> tuple[bool, bool]:
    """``(shapes, other)`` from executed payloads, not request ``content``.

    A line that is only a ``"content"`` value is the python-domain few-shot
    (or a later replay of that prompt). Arguments and ``Tool call:`` lines
    are the host's record of a call. Indent=2 sync responses put ``content``
    and ``arguments`` on different lines, so skipping content lines keeps
    the few-shot out without dropping the tool-call payload.
    """
    shapes = False
    other = False
    for line in log_text.splitlines():
        if _PROSE_KEY_RE.search(line) or not _line_carries_call_payload(line):
            continue
        decoded = _unescape_args(line)
        if _SHAPES_DOMAIN_RE.search(decoded):
            shapes = True
        elif _DOMAINS_LIST_RE.search(decoded):
            other = True
    return shapes, other


def _upsert_after_dtd(log_text: str, dtd_pos: int) -> bool:
    """Executed ``run_venv`` after DTD, plus ``wa.shape.upsert`` not in content.

    Both have to show up after the Smol / tool-async line. The shapes
    few-shot is a ``"content"`` line and is ignored even when the next
    request dumps it below the execution.
    """
    tail = log_text[dtd_pos:]
    newline = tail.find("\n")
    if newline < 0:
        return False
    saw_venv = False
    saw_upsert = False
    for line in tail[newline + 1:].splitlines():
        if _PROSE_KEY_RE.search(line):
            continue
        decoded = _unescape_args(line)
        if _is_run_venv_exec_line(line):
            saw_venv = True
            if "wa.shape.upsert" in decoded:
                saw_upsert = True
        elif "wa.shape.upsert" in decoded:
            saw_upsert = True
    return saw_venv and saw_upsert


def _empty_dtd_counts_as_shapes(log_text: str, calls: list[_LoggedCall]) -> bool:
    """Name-only nested DTD counts as shapes only with executed shapes evidence.

    What was wrong: ``SmolToolAdapter executing … 'delegate_tool_domains'``
    and ``tool-async-delegate_tool_domains`` were stored as ``(name, "")``.
    The shapes check searched those empty args, so a host that really ran
    the nested delegation (Gemini 3.5 Flash Lite stamp 20260927-0019) failed
    with only "missing delegate_tool_domains with domains including shapes"
    while python, ``run_venv``, and a 13-stripe page were already yes.

    How: the nested agent is sync. The worker logs arguments on
    ``=== Sync response:`` ``tool_calls`` (often above the Smol line) or
    not at all when the adapter line is the only execution record.

    Why this fixes it: arguments on that sync payload count. When the
    adapter line is all we have, shapes counts if a non-content executed
    payload shows ``domains`` including shapes. If no executed payload
    names a domain list, shapes also counts when an executed
    ``run_venv_python_script`` after that DTD places shapes
    (``wa.shape.upsert``). An explicit domains list without shapes
    (footnotes-only) does not, even if the prompt few-shot mentions both.
    """
    dtd_pos = _dtd_exec_pos(log_text)
    has_empty = dtd_pos is not None or any(
        call.name == "delegate_tool_domains" and not call.args.strip() for call in calls
    )
    if not has_empty:
        return False
    # Captured arguments win over a nearby shapes mention. Footnotes-only
    # tool_calls stay false even when the prompt example or a later
    # wa.shape.upsert line is in the same log.
    saw_shapes_args = False
    saw_other_args = False
    for call in calls:
        if call.name != "delegate_tool_domains" or not call.args.strip():
            continue
        if _SHAPES_DOMAIN_RE.search(_unescape_args(call.args)):
            saw_shapes_args = True
        else:
            saw_other_args = True
    if saw_shapes_args:
        return True
    if saw_other_args:
        return False
    shapes_line, other_line = _domain_markers_outside_content(log_text)
    if shapes_line:
        return True
    if other_line:
        return False
    for call in calls:
        if call.name != "run_venv_python_script" or "wa.shape.upsert" not in _unescape_args(call.args):
            continue
        # Sync-response args are the script that ran; the Smol line can flush later.
        if call.from_sync or (dtd_pos is not None and call.pos > dtd_pos):
            return True
    if dtd_pos is None:
        return False
    return _upsert_after_dtd(log_text, dtd_pos)


def _is_python_delegate(name: str, args: str) -> bool:
    if not (name.startswith("delegate_to_specialized_") and name.endswith("_toolset")):
        return False
    return _PYTHON_DOMAIN_RE.search(_unescape_args(args)) is not None


_IMAGES_DOMAINS_RE = re.compile(
    r"""["']domains["']\s*:\s*\[[^\]]*["']images["']""",
    re.IGNORECASE,
)


def _is_images_path(name: str, args: str) -> bool:
    decoded = _unescape_args(args)
    if name in _IMAGE_TOOL_NAMES:
        return True
    if name.startswith("delegate_to_specialized_") and name.endswith("_toolset"):
        return _IMAGES_DOMAIN_RE.search(decoded) is not None
    # delegate_tool_domains(domains=["images"]) is the same wrong route.
    if name == "delegate_tool_domains" and _IMAGES_DOMAINS_RE.search(decoded):
        return True
    return False


def parse_path_evidence(log_text: str) -> PathEvidence:
    """Read process evidence from a debug log. Empty text is no evidence."""
    text = log_text or ""
    calls = _calls_from_log(text)
    python_domain = False
    delegate_shapes = False
    run_venv = False
    llm_shape_upsert = False
    images_path = False
    script_placement = False
    shape_group_call = False
    for call in calls:
        name = call.name
        args = call.args
        decoded = _unescape_args(args)
        if _is_python_delegate(name, args):
            python_domain = True
        if name == "delegate_tool_domains" and _SHAPES_DOMAIN_RE.search(decoded):
            delegate_shapes = True
        if name == "run_venv_python_script":
            run_venv = True
            if "wa.shape.upsert" in decoded:
                script_placement = True
        if name == "shape_upsert":
            llm_shape_upsert = True
        if _is_images_path(name, args):
            images_path = True
        if name == "shape_group" or "wa.shape.group" in decoded:
            shape_group_call = True
    # Nested DTD is executed with empty args on the Smol / tool-async line.
    # Infer shapes only from later executed evidence, not from few-shot content.
    if not delegate_shapes and _empty_dtd_counts_as_shapes(text, calls):
        delegate_shapes = True
    if any(marker in text for marker in _CREATE_SHAPE_MARKERS):
        script_placement = True
    return PathEvidence(
        python_domain=python_domain,
        delegate_shapes=delegate_shapes,
        run_venv=run_venv,
        llm_shape_upsert=llm_shape_upsert,
        images_path=images_path,
        script_placement=script_placement,
        shape_group_call=shape_group_call,
    )


def evidence_from_eval_trace(trace: list[dict[str, object]] | None) -> PathEvidence:
    """Path checks from the eval-1 harness trace, not ``writeragent_debug.log``.

    Each entry is one executed tool call (``name`` + JSON ``arguments``),
    including nested specialized rounds. Few-shot text never appears here,
    so the headed log's content-line filter is unnecessary. A missing trace
    is empty evidence; the caller decides whether that fails closed.
    """
    python_domain = False
    delegate_shapes = False
    run_venv = False
    llm_shape_upsert = False
    images_path = False
    script_placement = False
    shape_group_call = False
    for entry in trace or []:
        name = str(entry.get("name") or "")
        args = entry.get("arguments")
        blob = args if isinstance(args, str) else ""
        decoded = _unescape_args(blob)
        if _is_python_delegate(name, blob):
            python_domain = True
        if name == "delegate_tool_domains" and _SHAPES_DOMAIN_RE.search(decoded):
            delegate_shapes = True
        if name == "run_venv_python_script":
            run_venv = True
            if "wa.shape.upsert" in decoded:
                script_placement = True
        if name == "shape_upsert":
            llm_shape_upsert = True
        if _is_images_path(name, blob):
            images_path = True
        if name == "shape_group" or "wa.shape.group" in decoded:
            shape_group_call = True
    return PathEvidence(
        python_domain=python_domain,
        delegate_shapes=delegate_shapes,
        run_venv=run_venv,
        llm_shape_upsert=llm_shape_upsert,
        images_path=images_path,
        script_placement=script_placement,
        shape_group_call=shape_group_call,
    )


def parse_tool_rounds(log_text: str) -> tuple[int | None, int | None]:
    """Outer tool-loop budget and rounds used. Not a pass/fail input.

    ``Tool-calling loop START (max N rounds)`` is INFO. ``Tool loop round
    N`` is DEBUG and 0-based, so used is max index + 1. A later START
    (sidebar re-entry) drops earlier rounds. Missing lines stay None.
    """
    if not log_text:
        return None, None
    starts = list(_LOOP_START_RE.finditer(log_text))
    if not starts:
        indexes = [int(match.group(1)) for match in _LOOP_ROUND_RE.finditer(log_text)]
        used = (max(indexes) + 1) if indexes else None
        return used, None
    last = starts[-1]
    budget = int(last.group(1))
    indexes = [int(match.group(1)) for match in _LOOP_ROUND_RE.finditer(log_text[last.end():])]
    used = (max(indexes) + 1) if indexes else None
    return used, budget


def extract_preview_png(doc: Path) -> Path | None:
    """Copy the page thumbnail LibreOffice stored in the ``.odt``.

    A Writer ``storeToURL`` writes ``Thumbnails/thumbnail.png``. That file
    is the board picture for the model entry. A hand-built zip with no
    thumbnail returns None and does not fail the score.
    """
    try:
        with zipfile.ZipFile(doc) as zf:
            match = next(
                (name for name in zf.namelist() if name.replace("\\", "/").lower() == _ODF_THUMBNAIL.lower()),
                None,
            )
            if match is None:
                return None
            data = zf.read(match)
    except (OSError, zipfile.BadZipFile, KeyError):
        return None
    if not data.startswith(_PNG_MAGIC):
        return None
    dest = doc.parent / PREVIEW_PNG_NAME
    dest.write_bytes(data)
    return dest


def quality_partial(failures: list[str], soft: list[str]) -> float:
    """Hard failures plus at most one soft star note, over a fixed denominator.

    A page-scale striped flag with messy stars stays high (strong partial)
    and can still ``pass``. A blank, speck, or wrong path does not.
    """
    denom = CHECK_COUNT + SOFT_CHECK_COUNT
    used = len(failures) + len(soft)
    return round(max(0.0, 1.0 - used / denom), 4)


def score_flag(
    geometry: FlagGeometry | None,
    evidence: PathEvidence,
    *,
    geometry_error: str | None = None,
    log_missing: bool = False,
) -> OracleResult:
    """Hard gate: path + page-scale stripes. Stars are soft, not a fail."""
    failures: list[str] = []
    geom = geometry or FlagGeometry(0, 0, 0, 0, False)
    if log_missing:
        failures.append("writeragent_debug.log missing; no python specialized path")
        failures.append("writeragent_debug.log missing; no delegate_tool_domains(shapes)")
        failures.append("writeragent_debug.log missing; no run_venv_python_script")
    else:
        if not evidence.python_domain:
            failures.append("missing python specialized path (delegate domain=python)")
        if not evidence.delegate_shapes:
            failures.append("missing delegate_tool_domains with domains including shapes")
        if not evidence.run_venv:
            failures.append("missing run_venv_python_script execution")
    # Bad substitutes fail even when the log is missing only if we saw them.
    # A missing log has no such evidence, so these two stay quiet (the three
    # path checks above already fail closed).
    if evidence.llm_shape_upsert and not evidence.run_venv:
        failures.append("LLM shape_upsert without run_venv_python_script")
    if evidence.images_path:
        failures.append("domain=images / image_generate PNG path (shapes script required)")
    if geometry_error:
        failures.append(f"cannot read Writer document: {geometry_error}")
        failures.append("stripe-like rects unavailable (document unreadable)")
        failures.append("page-scale width unavailable (document unreadable)")
    else:
        if geom.shape_count == 0:
            failures.append("blank page (no drawing shapes)")
        elif geom.stripe_rects < MIN_STRIPE_RECTS:
            failures.append(
                f"stripe-like rects {geom.stripe_rects} < {MIN_STRIPE_RECTS} "
                f"(width/height >= {STRIPE_MIN_ASPECT:g}; need a page-scale striped field)"
            )
        if geom.max_width_hmm < MIN_MAX_WIDTH_HMM:
            failures.append(
                f"max width {geom.max_width_hmm} HMM < {MIN_MAX_WIDTH_HMM} "
                "(speck; page-scale flag is about 10000–20000 HMM, not ~1900)"
            )
    # Soft only when the flag is actually attempted at page scale. A blank
    # or speck already failed; do not also ding its stars.
    soft: list[str] = []
    page_scale_stripes = (
        geometry_error is None
        and geom.stripe_rects >= MIN_STRIPE_RECTS
        and geom.max_width_hmm >= MIN_MAX_WIDTH_HMM
    )
    if page_scale_stripes and geom.star_shapes < SOFT_STAR_FIELD:
        canton = ""
        if geom.canton_rects:
            canton = f"; canton-like rects {geom.canton_rects}"
        soft.append(
            f"star-like shapes {geom.star_shapes} < {SOFT_STAR_FIELD}{canton} "
            "(soft: messy or under-counted stars still pass; "
            "gpt-oss-20b almost-flag is the matrix floor, not a 50-star canton)"
        )
    bonuses: list[str] = []
    grouped = geom.grouped or evidence.shape_group_call
    if grouped:
        bonuses.append("shape_group / draw:g (bonus; not required, #927)")
    if evidence.script_placement:
        bonuses.append("wa.shape.upsert / create_shape in the run log (bonus)")
    return OracleResult(
        passed=not failures,
        failures=failures,
        soft=soft,
        checks=CHECK_COUNT,
        partial_score=quality_partial(failures, soft),
        bonuses=bonuses,
        shape_count=geom.shape_count,
        stripe_rects=geom.stripe_rects,
        star_shapes=geom.star_shapes,
        canton_rects=geom.canton_rects,
        max_width_hmm=geom.max_width_hmm,
        grouped=grouped,
        python_domain=evidence.python_domain,
        delegate_shapes=evidence.delegate_shapes,
        run_venv=evidence.run_venv,
        llm_shape_upsert=evidence.llm_shape_upsert,
        images_path=evidence.images_path,
        script_placement=evidence.script_placement,
    )


def _first_existing(*candidates: Path) -> Path | None:
    seen: set[Path] = set()
    for path in candidates:
        key = path.resolve() if path.exists() else path
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            return path
    return None


def resolve_flag_artifact(artifact: Path) -> tuple[Path | None, Path | None]:
    """Pick the Writer ``.odt`` and sibling debug log from a file or run dir."""
    artifact = artifact.expanduser()
    if artifact.is_dir():
        untitled = sorted(
            path for path in artifact.glob("*.odt") if path.name.lower().startswith("untitled")
        )
        doc = _first_existing(
            *[artifact / name for name in _PREFERRED_ODT],
            *untitled,
            *sorted(artifact.glob("*.odt")),
        )
        log_path = artifact / DEBUG_LOG_FILENAME
        return doc, (log_path if log_path.is_file() else None)
    if artifact.name == DEBUG_LOG_FILENAME:
        doc, _log = resolve_flag_artifact(artifact.parent)
        return doc, (artifact if artifact.is_file() else None)
    if artifact.suffix.lower() == ".odt":
        log_path = artifact.parent / DEBUG_LOG_FILENAME
        return (artifact if artifact.is_file() else None), (log_path if log_path.is_file() else None)
    return None, None


def score_artifact(path: Path | str) -> OracleResult:
    """Score a saved ``.odt``, a debug log, or a run directory that holds both."""
    doc, log_path = resolve_flag_artifact(Path(path))
    geometry: FlagGeometry | None = None
    geometry_error: str | None = None
    if doc is None:
        geometry_error = f"need a Writer .odt (artifact={path})"
    elif doc.suffix.lower() != ".odt":
        geometry_error = f"Writer deliverable required (.odt), not {doc.suffix}"
    else:
        try:
            geometry = read_writer_flag(doc)
        except Exception as exc:
            geometry_error = str(exc)
    log_text = ""
    log_missing = log_path is None
    if log_path is not None:
        try:
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            log_missing = True
            log_text = ""
    evidence = parse_path_evidence(log_text)
    result = score_flag(
        geometry,
        evidence,
        geometry_error=geometry_error,
        log_missing=log_missing,
    )
    # Recorded after the hard gate so a round count cannot add a failure.
    used, budget = parse_tool_rounds(log_text)
    result.tool_rounds_used = used
    result.tool_rounds_budget = budget
    if doc is not None and doc.is_file():
        preview = extract_preview_png(doc)
        result.preview_png = str(preview) if preview is not None else None
    return result


def format_result(result: OracleResult) -> str:
    status = "PASS" if result.passed else "FAIL"
    lines = [
        status,
        (
            f"  shapes: {result.shape_count}  stripes: {result.stripe_rects}  "
            f"stars: {result.star_shapes}  canton_rects: {result.canton_rects}  "
            f"max_width_hmm: {result.max_width_hmm}  "
            f"grouped: {'yes' if result.grouped else 'no'}"
        ),
        (
            f"  path: python={_yn(result.python_domain)}  "
            f"delegate_shapes={_yn(result.delegate_shapes)}  "
            f"run_venv={_yn(result.run_venv)}  "
            f"hard: {len(result.failures)}/{result.checks} failed  "
            f"partial: {result.partial_score:.3f}"
        ),
        (
            "  tool_rounds: "
            f"{_rounds_label(result.tool_rounds_used, result.tool_rounds_budget)} "
            "(recorded, not scored)"
        ),
        f"  preview: {result.preview_png or 'none'}",
    ]
    for item in result.bonuses:
        lines.append(f"  bonus: {item}")
    for item in result.soft:
        lines.append(f"  soft: {item}")
    for item in result.failures:
        lines.append(f"  - {item}")
    return "\n".join(lines)


def _yn(flag: bool) -> str:
    return "yes" if flag else "no"


def _rounds_label(used: int | None, budget: int | None) -> str:
    if used is None and budget is None:
        return "n/a"
    used_text = "?" if used is None else str(used)
    budget_text = "?" if budget is None else str(budget)
    return f"{used_text}/{budget_text}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "artifact",
        type=Path,
        help="Saved Writer .odt, writeragent_debug.log, or run directory",
    )
    parser.add_argument("--json", action="store_true", help="Print the result as JSON")
    args = parser.parse_args(argv)
    result = score_artifact(args.artifact)
    if args.json:
        print(json.dumps(result.to_json(), indent=2))
    else:
        print(format_result(result))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

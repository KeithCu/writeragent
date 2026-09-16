# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Impress shipped-design tools (list / create-from-template / core setup).

M0′ (``docs/draw/impress-lo-first-m0-probe-results.md``, PR #788) showed:

* PathSettings template dirs enumerate shipped ``.otp`` (Metropolis, …).
* ``loadComponentFromURL`` + ``AsTemplate`` creates a designed **new** doc.
* ``loadStylesFromURL`` is absent/incomplete on Impress — current-doc apply
  cannot reproduce a full ``.otp`` look. Do not fake it.

``set_presentation_design`` is the main-chat one-shot: new doc from a listed
design, master assignment, headers/footers + slide numbers.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from plugin.doc.document_research import (
    _path_settings_from_ctx,
    _resolve_lo_directory_path,
    _should_skip_filename,
)
from plugin.framework.tool import ToolBase, ToolContext
from plugin.framework.url_utils import path_to_file_url

log = logging.getLogger("writeragent.draw.designs")

# Structured error: current-doc apply is an LO wall (M0′). Not TOOL_EXECUTION_ERROR.
LO_WALL_CURRENT_DOC_APPLY = "LO_WALL"
NOT_IMPRESS_CODE = "UNSUPPORTED_DOC_TYPE"

# PathSettings properties that hold template directories. Discover extras whose
# name contains "emplate" so we do not hardcode an install prefix.
_TEMPLATE_PROP_FALLBACK = (
    "Template",
    "Template_internal",
    "Template_user",
    "Template_writable",
)

_OTP_EXT = ".otp"

_CURRENT_DOC_APPLY_MESSAGE = (
    "LibreOffice cannot apply a full .otp design onto the current presentation. "
    "loadStylesFromURL is absent/incomplete on Impress (M0′); master insert is "
    "not a full template import. Open a new document from the design instead "
    "(apply_design new_document=true, or set_presentation_design)."
)


def _is_impress_doc(doc: Any) -> bool:
    """True for Impress. PyUNO hasattr is unreliable (same as pages._is_impress_doc)."""
    try:
        return bool(doc.supportsService("com.sun.star.presentation.PresentationDocument"))
    except Exception:
        return False


def _is_draw_only_doc(doc: Any) -> bool:
    """True for Draw that is not also a presentation."""
    try:
        if _is_impress_doc(doc):
            return False
        return bool(doc.supportsService("com.sun.star.drawing.DrawingDocument"))
    except Exception:
        return False


def current_doc_apply_error(tool: ToolBase) -> dict[str, Any]:
    """Structured LO-wall payload for current-document apply-design."""
    return tool._tool_error(
        _CURRENT_DOC_APPLY_MESSAGE,
        code=LO_WALL_CURRENT_DOC_APPLY,
        reason="current_doc_apply_unsupported",
        evidence="m0_loadStylesFromURL_absent",
        hint="Use set_presentation_design or apply_design with new_document=true.",
    )


def not_impress_error(tool: ToolBase, action: str) -> dict[str, Any]:
    return tool._tool_error(
        "%s requires an Impress presentation, not a Draw document." % action,
        code=NOT_IMPRESS_CODE,
        doc_type="draw",
    )


def _template_property_names(settings: Any) -> list[str]:
    """Discover PathSettings template properties; never a filesystem prefix."""
    names: list[str] = []
    try:
        info = settings.getPropertySetInfo()
        if info is not None:
            for prop in info.getProperties():
                n = str(getattr(prop, "Name", "") or "")
                if n and "emplate" in n and n not in names:
                    names.append(n)
    except Exception:
        log.debug("PathSettings getPropertySetInfo failed", exc_info=True)
    for n in _TEMPLATE_PROP_FALLBACK:
        if n not in names:
            names.append(n)
    return names


def _split_pathsettings_value(raw: str) -> list[str]:
    """Split a PathSettings value into file URLs or filesystem paths.

    LO typically space-separates ``file://`` URLs; some builds use ``;``.
    """
    text = str(raw or "").strip()
    if not text:
        return []
    if ";" in text:
        return [p.strip() for p in text.split(";") if p.strip()]
    if "file:" in text:
        parts: list[str] = []
        buf = ""
        for token in text.split():
            if token.startswith("file:"):
                if buf:
                    parts.append(buf)
                buf = token
            elif buf:
                # Encoded spaces are %20; a raw space after file: starts a new URL.
                if token.startswith("file:"):
                    parts.append(buf)
                    buf = token
                else:
                    buf = buf + " " + token
            else:
                parts.append(token)
        if buf:
            parts.append(buf)
        return parts
    return [p for p in text.split() if p]


def _iter_template_directories(ctx: Any) -> list[str]:
    """Resolve existing template directories from PathSettings (no install prefix)."""
    settings = _path_settings_from_ctx(ctx)
    if settings is None:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for prop_name in _template_property_names(settings):
        raw: Any = None
        try:
            raw = settings.getPropertyValue(prop_name)
        except Exception:
            raw = getattr(settings, prop_name, None)
        if raw is None:
            continue
        for chunk in _split_pathsettings_value(str(raw)):
            resolved = _resolve_lo_directory_path(ctx, chunk)
            if resolved is None:
                continue
            key = os.path.normcase(resolved)
            if key in seen:
                continue
            seen.add(key)
            found.append(resolved)
    return found


def _design_id_from_path(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0].strip().lower()


def _design_name_from_path(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def enumerate_impress_designs(ctx: Any) -> list[dict[str, str]]:
    """Walk PathSettings template dirs for ``.otp`` files. Stable id = stem lowercased."""
    designs: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for directory in _iter_template_directories(ctx):
        for root, _unused_dirs, files in os.walk(directory):
            for filename in files:
                if _should_skip_filename(filename):
                    continue
                if not filename.lower().endswith(_OTP_EXT):
                    continue
                full = os.path.normpath(os.path.join(root, filename))
                key = os.path.normcase(full)
                if key in seen_paths:
                    continue
                if not os.path.isfile(full):
                    continue
                seen_paths.add(key)
                designs.append(
                    {
                        "id": _design_id_from_path(full),
                        "name": _design_name_from_path(full),
                        "path": full,
                        "url": path_to_file_url(full),
                    }
                )
    designs.sort(key=lambda d: (d["name"].lower(), d["path"]))
    return designs


def resolve_design(ctx: Any, design: str) -> dict[str, str] | None:
    """Match a listed design by id, name, path, or file URL."""
    needle = str(design or "").strip()
    if not needle:
        return None
    designs = enumerate_impress_designs(ctx)
    lower = needle.lower()
    path_norm = os.path.normcase(os.path.normpath(needle)) if not needle.startswith("file:") else ""
    for entry in designs:
        if entry["id"] == lower or entry["name"].lower() == lower:
            return entry
        if path_norm and os.path.normcase(entry["path"]) == path_norm:
            return entry
        if needle.startswith("file:") and entry["url"] == needle:
            return entry
    return None


def _master_entries(doc: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        masters = doc.getMasterPages()
    except Exception:
        return out
    for i in range(masters.getCount()):
        m = masters.getByIndex(i)
        name = m.Name if hasattr(m, "Name") else ""
        shapes = 0
        try:
            shapes = int(m.getCount())
        except Exception:
            pass
        out.append({"index": i, "name": name or "", "shape_count": shapes})
    return out


def _blank_master_signal(masters: list[dict[str, Any]]) -> bool:
    """True when the deck looks like factory Default (white / empty-ish master)."""
    if not masters:
        return True
    names = [str(m.get("name") or "").strip().lower() for m in masters]
    if any(n and n != "default" for n in names):
        return False
    return all(int(m.get("shape_count") or 0) < 3 for m in masters)


def assign_primary_master_to_slides(doc: Any) -> str:
    """Assign the first (design) master to every slide. Returns the master name."""
    masters = doc.getMasterPages()
    if masters.getCount() < 1:
        return ""
    target = masters.getByIndex(0)
    name = target.Name if hasattr(target, "Name") else ""
    pages = doc.getDrawPages()
    for i in range(pages.getCount()):
        try:
            pages.getByIndex(i).MasterPage = target
        except Exception:
            log.debug("assign_primary_master_to_slides failed on page %s", i, exc_info=True)
    return name or ""


def inherit_master_from_neighbor(pages: Any, new_page: Any, insert_at: int) -> str:
    """Copy MasterPage from the slide that was adjacent at insert time.

    ``insertNewByIndex`` can leave a factory Default master even when the deck
    already has an assigned design master. Inherit from the previous slide
    (or the slide now after the insert) so add_slide keeps the deck look.
    """
    try:
        count = int(pages.getCount())
    except Exception:
        return ""
    if count < 2:
        return ""
    ref_idx = insert_at - 1 if insert_at > 0 else insert_at + 1
    if ref_idx < 0 or ref_idx >= count or ref_idx == insert_at:
        return ""
    try:
        ref = pages.getByIndex(ref_idx)
        master = ref.MasterPage
        new_page.MasterPage = master
        return master.Name if hasattr(master, "Name") else ""
    except Exception:
        log.debug("inherit_master_from_neighbor failed insert_at=%s", insert_at, exc_info=True)
        return ""


def create_presentation_from_design(
    uno_ctx: Any,
    design: dict[str, str],
    *,
    hidden: bool = False,
) -> Any:
    """``loadComponentFromURL`` + ``AsTemplate`` — new document only (M0′ proven path)."""
    from plugin.framework.thread_guard import guard_uno
    from plugin.framework.uno_context import get_desktop
    from plugin.writer.format import create_property_value

    desktop = get_desktop(uno_ctx)
    if desktop is None:
        raise RuntimeError("LibreOffice desktop is not available.")
    url = design.get("url") or path_to_file_url(design["path"])
    props = [create_property_value("AsTemplate", True)]
    if hidden:
        props.append(create_property_value("Hidden", True))
    # Unique CREATE|GLOBAL target for Hidden loads (leftover ``_blank`` hangs on
    # some Windows GHA runs). Visible product opens stay ``_blank``.
    target = "_wa_impress_otp" if hidden else "_blank"
    model = desktop.loadComponentFromURL(url, target, 0, tuple(props))
    if model is None:
        raise RuntimeError("loadComponentFromURL returned no document for %s" % url)
    return guard_uno(model)


def enable_design_headers_footers(new_doc: Any, uno_ctx: Any, services: Any) -> dict[str, Any]:
    """Reuse ``set_headers_footers`` on the new doc's assigned master + first slide."""
    from plugin.draw.headers_footers import SetHeadersFooters

    hf_ctx = ToolContext(new_doc, uno_ctx, "impress", services, caller="set_presentation_design")
    tool = SetHeadersFooters()
    master = tool.execute(
        hf_ctx,
        page=0,
        is_master_page=True,
        footer_visible=True,
        page_number_visible=True,
        header_visible=True,
    )
    slide = tool.execute(
        hf_ctx,
        page=0,
        is_master_page=False,
        footer_visible=True,
        page_number_visible=True,
    )
    return {"master": master, "slide": slide}


def _new_doc_payload(new_doc: Any, design: dict[str, str], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    from plugin.framework.uno_context import get_runtime_uid

    masters = _master_entries(new_doc)
    url = ""
    try:
        url = str(new_doc.getURL() or "")
    except Exception:
        url = ""
    payload: dict[str, Any] = {
        "status": "ok",
        "design": design,
        "document_url": url,
        "document_uid": get_runtime_uid(new_doc),
        "masters": masters,
        "blank_master": _blank_master_signal(masters),
        "message": (
            "Opened a new presentation from design '%s'. "
            "Pass document_url or document_uid to later tools to target it."
            % design.get("name")
        ),
    }
    if extra:
        payload.update(extra)
    return payload


class ListDesigns(ToolBase):
    """Enumerate shipped Impress ``.otp`` designs via PathSettings."""

    name = "list_designs"
    intent = "navigate"
    description = (
        "List shipped Impress .otp designs from LibreOffice PathSettings template "
        "directories (id, name, path, url). Use a listed id with set_presentation_design "
        "to start a new deck. Does not hardcode the install prefix."
    )
    parameters = {"type": "object", "properties": {}, "required": []}
    uno_services = [
        "com.sun.star.drawing.DrawingDocument",
        "com.sun.star.presentation.PresentationDocument",
    ]
    tier = "core"
    is_mutation = False

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        designs = enumerate_impress_designs(ctx.ctx)
        return {"status": "ok", "designs": designs, "count": len(designs)}


class ApplyDesign(ToolBase):
    """Create a new presentation from a listed ``.otp``, or LO-wall on current-doc apply."""

    name = "apply_design"
    intent = "edit"
    description = (
        "Apply a listed Impress design by creating a NEW presentation from the .otp "
        "(loadComponentFromURL + AsTemplate). Set new_document=true (default). "
        "Current-document apply is a LibreOffice wall (loadStylesFromURL absent) and "
        "returns a structured LO_WALL error — do not expect the open deck to restyle. "
        "Draw documents return a not-Impress error."
    )
    parameters = {
        "type": "object",
        "properties": {
            "design": {
                "type": "string",
                "description": "Design id, name, path, or url from list_designs (e.g. Metropolis).",
            },
            "new_document": {
                "type": "boolean",
                "description": (
                    "Must be true to create a new presentation from the template. "
                    "false / apply to the current document returns LO_WALL."
                ),
            },
        },
        "required": ["design"],
    }
    uno_services = [
        "com.sun.star.drawing.DrawingDocument",
        "com.sun.star.presentation.PresentationDocument",
    ]
    tier = "core"
    is_mutation = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.headers_footers import _coerce_bool_arg

        if _is_draw_only_doc(ctx.doc):
            return not_impress_error(self, "apply_design")
        new_document = True
        if "new_document" in kwargs:
            new_document = _coerce_bool_arg(kwargs, "new_document", True)
        if not new_document:
            return current_doc_apply_error(self)
        design_key = str(kwargs.get("design") or "").strip()
        if not design_key:
            return self._tool_error("design is required.")
        entry = resolve_design(ctx.ctx, design_key)
        if entry is None:
            available = [d["id"] for d in enumerate_impress_designs(ctx.ctx)]
            return self._tool_error("Design not found: %s" % design_key, available=available[:40])
        try:
            # ``hidden`` is for native tests (leftover visible frames). Not a product arg.
            hidden = bool(kwargs.get("hidden"))
            new_doc = create_presentation_from_design(ctx.ctx, entry, hidden=hidden)
        except Exception as e:
            log.exception("create-from-template failed for %s", entry.get("path"))
            return self._tool_error("Failed to open design as template: %s" % e)
        return _new_doc_payload(new_doc, entry)


class SetPresentationDesign(ToolBase):
    """Core one-shot: new Impress from design + master + HF / slide numbers."""

    name = "set_presentation_design"
    intent = "edit"
    description = (
        "Start a new Impress presentation from a shipped .otp design (create-from-template), "
        "assign the design master, and enable headers/footers plus slide numbers. "
        "Does not restyle the current document (LibreOffice wall). "
        "Call list_designs first. Draw documents return a not-Impress error."
    )
    parameters = {
        "type": "object",
        "properties": {
            "design": {
                "type": "string",
                "description": "Design id or name from list_designs (e.g. Metropolis).",
            },
        },
        "required": ["design"],
    }
    uno_services = [
        "com.sun.star.drawing.DrawingDocument",
        "com.sun.star.presentation.PresentationDocument",
    ]
    tier = "core"
    is_mutation = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        if _is_draw_only_doc(ctx.doc):
            return not_impress_error(self, "set_presentation_design")
        if ctx.doc is not None and not _is_impress_doc(ctx.doc) and not _is_draw_only_doc(ctx.doc):
            return not_impress_error(self, "set_presentation_design")
        design_key = str(kwargs.get("design") or "").strip()
        if not design_key:
            return self._tool_error("design is required.")
        entry = resolve_design(ctx.ctx, design_key)
        if entry is None:
            available = [d["id"] for d in enumerate_impress_designs(ctx.ctx)]
            return self._tool_error("Design not found: %s" % design_key, available=available[:40])
        try:
            hidden = bool(kwargs.get("hidden"))
            new_doc = create_presentation_from_design(ctx.ctx, entry, hidden=hidden)
        except Exception as e:
            log.exception("set_presentation_design create-from-template failed")
            return self._tool_error("Failed to open design as template: %s" % e)
        master_name = assign_primary_master_to_slides(new_doc)
        hf = enable_design_headers_footers(new_doc, ctx.ctx, ctx.services)
        return _new_doc_payload(
            new_doc,
            entry,
            extra={"assigned_master": master_name, "headers_footers": hf},
        )

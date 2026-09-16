# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Impress shipped-design tools (list / create-from-template / current-doc apply).

M0′ (``docs/draw/impress-lo-first-m0-probe-results.md``, PR #788) showed:

* PathSettings template dirs enumerate shipped ``.otp`` (Metropolis, …).
* ``loadComponentFromURL`` + ``AsTemplate`` creates a designed **new** doc.
* ``loadStylesFromURL`` is absent/incomplete on Impress — do not use it.

Current-doc restyle is the load-master probe path (not ``.uno:PresentationLayout``
PropertyValues — SDI formal args are empty, so those dispatches silent-no-op):

1. Hidden-open the listed ``.otp``.
2. Slide-sorter clipboard: src ``.uno:DiaMode`` + ``.uno:Copy`` → dest
   ``.uno:DiaMode`` + ``.uno:Paste`` → ``.uno:DrawingMode``. That is the
   reachable equivalent of dialog **Load** (imports the full master).
3. ``page.MasterPage = imported_master`` on every slide (Exchange-all).
4. Drop any extra content slide the paste added; close the hidden source.

Do not deep-copy master shapes (wrong title geometry; GraphicObjectShape
IllegalArgument) or assign a master from another document (no-op / unsafe).

``set_presentation_design`` stays the main-chat one-shot: **new** doc from a
listed design, master assignment, headers/footers + slide numbers.

``list_designs`` includes a short ``look`` string (mood / accent hues /
illustrated vs graphic chrome) derived from the ``.otp`` ZIP — see
``design_look.derive_otp_look``. No Desktop open, no vision images.
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
from plugin.draw.design_look import derive_otp_look
from plugin.framework.tool import ToolBase, ToolContext
from plugin.framework.url_utils import path_to_file_url

log = logging.getLogger("writeragent.draw.designs")

NOT_IMPRESS_CODE = "UNSUPPORTED_DOC_TYPE"
# Distinct from create-from-template's ``_wa_impress_otp`` so a hidden source
# load does not collide with a leftover new-doc Hidden frame.
_IMPORT_SOURCE_TARGET = "_wa_impress_otp_import"
_KEEP_SLIDE_PREFIX = "WA_KEEP_"

# PathSettings properties that hold template directories. Discover extras whose
# name contains "emplate" so we do not hardcode an install prefix.
_TEMPLATE_PROP_FALLBACK = (
    "Template",
    "Template_internal",
    "Template_user",
    "Template_writable",
)

_OTP_EXT = ".otp"


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
                        # ZIP thumbnail / Pictures / styles — never Desktop-open.
                        "look": derive_otp_look(full),
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


def _pump_uno_events(uno_ctx: Any) -> None:
    """Let DiaMode / paste settle. Probe dispatches worked without this; headed
    Adaption / view switches sometimes need a single idle pump.

    Use the approved chokepoint — raw toolkit.processEventsToIdle is lint-blocked.
    """
    try:
        from plugin.framework.uno_context import process_events_to_idle

        process_events_to_idle(uno_ctx)
    except Exception:
        log.debug("_pump_uno_events failed", exc_info=True)


def _dispatch_uno(uno_ctx: Any, doc: Any, command: str) -> None:
    """Dispatch a parameter-less ``.uno:*`` on *doc*'s frame (extension ctx)."""
    controller = doc.getCurrentController()
    if controller is None:
        raise RuntimeError("Document has no controller for %s" % command)
    frame = controller.getFrame()
    if frame is None:
        raise RuntimeError("Document has no frame for %s" % command)
    smgr = uno_ctx.ServiceManager
    dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", uno_ctx)
    if dispatcher is None:
        raise RuntimeError("DispatchHelper is not available.")
    dispatcher.executeDispatch(frame, command, "", 0, ())


def _close_hidden_doc(model: Any) -> None:
    """Close the Hidden .otp source. Prefer close(True); dispose only if close fails."""
    if model is None:
        return
    try:
        set_modified = getattr(model, "setModified", None)
        if callable(set_modified):
            set_modified(False)
    except Exception:
        log.debug("setModified(False) on hidden design source failed", exc_info=True)
    try:
        close_fn = getattr(model, "close", None)
        if callable(close_fn):
            close_fn(True)
            return
    except Exception:
        log.debug("close() on hidden design source failed", exc_info=True)
    try:
        dispose = getattr(model, "dispose", None)
        if callable(dispose):
            dispose()
    except Exception:
        log.exception("Failed to dispose hidden design source")


def open_design_source_hidden(uno_ctx: Any, design: dict[str, str], *, as_template: bool = True) -> Any:
    """Hidden-open a listed ``.otp`` as the DiaMode copy source.

    AsTemplate is optional in the probe; True matches the known-good control
    (full Metropolis master + SVG chrome). Distinct target from
    ``create_presentation_from_design``.
    """
    from plugin.framework.thread_guard import guard_uno
    from plugin.framework.uno_context import get_desktop
    from plugin.writer.format import create_property_value

    desktop = get_desktop(uno_ctx)
    if desktop is None:
        raise RuntimeError("LibreOffice desktop is not available.")
    url = design.get("url") or path_to_file_url(design["path"])
    props = [create_property_value("Hidden", True)]
    if as_template:
        props.append(create_property_value("AsTemplate", True))
    model = desktop.loadComponentFromURL(url, _IMPORT_SOURCE_TARGET, 0, tuple(props))
    if model is None:
        raise RuntimeError("loadComponentFromURL returned no document for %s" % url)
    return guard_uno(model)


def _mark_original_slides(doc: Any) -> tuple[list[tuple[str, str]], int]:
    """Tag current slides so a DiaMode paste extra can be removed later.

    Paste of the Hidden .otp typically appends one content slide on the
    imported master. Names are empty on factory decks, so we stamp a token
    and restore the prior name after cleanup.
    """
    pages = doc.getDrawPages()
    count = int(pages.getCount())
    marks: list[tuple[str, str]] = []
    for i in range(count):
        page = pages.getByIndex(i)
        prior = ""
        try:
            prior = str(page.Name or "")
        except Exception:
            prior = ""
        token = "%s%d" % (_KEEP_SLIDE_PREFIX, i)
        try:
            page.Name = token
            marks.append((token, prior))
        except Exception:
            log.debug("could not mark slide %s for paste cleanup", i, exc_info=True)
            marks.append(("", prior))
    return marks, count


def _restore_slide_names(doc: Any, marks: list[tuple[str, str]]) -> None:
    token_to_prior = {token: prior for token, prior in marks if token}
    if not token_to_prior:
        return
    pages = doc.getDrawPages()
    for i in range(pages.getCount()):
        page = pages.getByIndex(i)
        try:
            name = str(page.Name or "")
        except Exception:
            continue
        if name in token_to_prior:
            try:
                page.Name = token_to_prior[name]
            except Exception:
                log.debug("restore slide name failed index=%s", i, exc_info=True)


def remove_pasted_extra_slides(
    doc: Any,
    marks: list[tuple[str, str]],
    original_count: int,
) -> int:
    """Remove slides added by DiaMode paste; restore original names.

    Prefer token identity. If Name marks failed, extras are appended (probe).
    """
    keep = {mark[0] for mark in marks if mark[0]}
    pages = doc.getDrawPages()
    removed = 0
    if keep:
        for i in range(int(pages.getCount()) - 1, -1, -1):
            page = pages.getByIndex(i)
            try:
                name = str(page.Name or "")
            except Exception:
                name = ""
            if name in keep:
                continue
            try:
                pages.remove(page)
                removed += 1
            except Exception:
                log.debug("remove pasted extra slide failed index=%s", i, exc_info=True)
    else:
        while int(pages.getCount()) > original_count:
            try:
                pages.remove(pages.getByIndex(int(pages.getCount()) - 1))
                removed += 1
            except Exception:
                log.debug("remove appended extra slide failed", exc_info=True)
                break
    _restore_slide_names(doc, marks)
    return removed


def find_imported_master(
    doc: Any,
    design: dict[str, str],
    before_names: set[str],
) -> tuple[Any | None, str, int]:
    """Pick the master DiaMode paste imported (or the named design master)."""
    try:
        masters = doc.getMasterPages()
    except Exception:
        return None, "", 0
    design_name = str(design.get("name") or "").strip().lower()
    design_id = str(design.get("id") or "").strip().lower()
    entries: list[tuple[Any, str, int]] = []
    for i in range(masters.getCount()):
        m = masters.getByIndex(i)
        name = ""
        try:
            name = str(m.Name or "")
        except Exception:
            name = ""
        shapes = 0
        try:
            shapes = int(m.getCount())
        except Exception:
            shapes = 0
        entries.append((m, name, shapes))

    for m, name, shapes in entries:
        lower = name.strip().lower()
        if lower and (lower == design_name or lower == design_id):
            return m, name, shapes

    new_ones = [(m, name, shapes) for m, name, shapes in entries if name not in before_names]
    chrome = [row for row in new_ones if row[2] >= 6]
    pool = chrome or new_ones
    if pool:
        pool.sort(key=lambda row: (-row[2], row[1].lower()))
        return pool[0]
    return None, "", 0


def assign_master_to_all_slides(doc: Any, master: Any) -> int:
    """Exchange-background-page equivalent: assign *master* to every draw page."""
    pages = doc.getDrawPages()
    updated = 0
    for i in range(pages.getCount()):
        try:
            pages.getByIndex(i).MasterPage = master
            updated += 1
        except Exception:
            log.debug("assign_master_to_all_slides failed on page %s", i, exc_info=True)
    return updated


def apply_design_to_current_doc(uno_ctx: Any, dest_doc: Any, design: dict[str, str]) -> dict[str, Any]:
    """Import a shipped ``.otp`` master into *dest_doc* and assign it to all slides."""
    before_names = {str(m.get("name") or "") for m in _master_entries(dest_doc)}
    marks, original_count = _mark_original_slides(dest_doc)
    src = None
    dest_in_dia = False
    extras = 0
    import_error: Exception | None = None
    try:
        src = open_design_source_hidden(uno_ctx, design, as_template=True)
        _dispatch_uno(uno_ctx, src, ".uno:DiaMode")
        _pump_uno_events(uno_ctx)
        _dispatch_uno(uno_ctx, src, ".uno:Copy")
        _pump_uno_events(uno_ctx)
        _dispatch_uno(uno_ctx, dest_doc, ".uno:DiaMode")
        dest_in_dia = True
        _pump_uno_events(uno_ctx)
        _dispatch_uno(uno_ctx, dest_doc, ".uno:Paste")
        _pump_uno_events(uno_ctx)
    except Exception as exc:
        import_error = exc
    finally:
        if dest_in_dia:
            try:
                _dispatch_uno(uno_ctx, dest_doc, ".uno:DrawingMode")
                _pump_uno_events(uno_ctx)
            except Exception:
                log.debug("restore DrawingMode after design import failed", exc_info=True)
        _close_hidden_doc(src)
        try:
            extras = remove_pasted_extra_slides(dest_doc, marks, original_count)
        except Exception:
            extras = 0
            _restore_slide_names(dest_doc, marks)
    if import_error is not None:
        raise import_error
    master, master_name, shape_count = find_imported_master(dest_doc, design, before_names)
    if master is None:
        raise RuntimeError(
            "DiaMode paste did not import a design master from %s" % design.get("name")
        )
    slides_updated = assign_master_to_all_slides(dest_doc, master)
    masters = _master_entries(dest_doc)
    return {
        "status": "ok",
        "design": design,
        "applied_master": master_name,
        "applied_master_shape_count": shape_count,
        "masters": masters,
        "blank_master": _blank_master_signal(masters),
        "slides_updated": slides_updated,
        "pasted_slides_removed": extras,
        "message": (
            "Applied design '%s' (master '%s') to the current presentation."
            % (design.get("name"), master_name)
        ),
    }


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
        "directories (id, name, path, url, look). Read look to choose a design by "
        "appearance (dark/tech vs candy/illustrated), not only by name. Use a listed "
        "id with set_presentation_design to start a new deck. Does not hardcode the "
        "install prefix."
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
    """Apply a listed ``.otp`` to the current Impress deck, or create a new doc."""

    name = "apply_design"
    intent = "edit"
    description = (
        "Apply a listed Impress .otp design. new_document=false restyles the OPEN "
        "deck: Hidden-load the template, import its master via slide-sorter "
        "clipboard (DiaMode Copy/Paste), assign that master to every slide, and "
        "drop any extra pasted slide. Existing title/body text stays. "
        "new_document=true (default) still creates a NEW presentation "
        "(loadComponentFromURL + AsTemplate). Draw documents return a not-Impress error."
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
                    "true (default): create a new presentation from the template. "
                    "false: restyle the current Impress document onto the listed design."
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
        design_key = str(kwargs.get("design") or "").strip()
        if not design_key:
            return self._tool_error("design is required.")
        entry = resolve_design(ctx.ctx, design_key)
        if entry is None:
            available = [d["id"] for d in enumerate_impress_designs(ctx.ctx)]
            return self._tool_error("Design not found: %s" % design_key, available=available[:40])
        if not new_document:
            if not _is_impress_doc(ctx.doc):
                return not_impress_error(self, "apply_design")
            try:
                return apply_design_to_current_doc(ctx.ctx, ctx.doc, entry)
            except Exception as e:
                log.exception("current-doc apply_design failed for %s", entry.get("path"))
                return self._tool_error(
                    "Failed to apply design to the current presentation: %s" % e,
                    reason="current_doc_import_failed",
                    hint="Hidden .otp DiaMode import or MasterPage assign did not complete.",
                )
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
        "To restyle the open deck instead, use apply_design with new_document=false. "
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

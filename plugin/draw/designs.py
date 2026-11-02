# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Impress shipped-design tools (list / current-doc apply).

M0′ (``docs/draw/impress-lo-first-m0-probe-results.md``, PR #788) showed:

* PathSettings template dirs enumerate shipped ``.otp`` (Metropolis, …).
* ``loadComponentFromURL`` + ``AsTemplate`` creates a designed **new** doc
  (kept as ``create_presentation_from_design`` for tests only — not a tool).
* ``loadStylesFromURL`` is absent/incomplete on Impress — do not use it.

Current-doc restyle (#791 clipboard path failed headed — system clipboard
paste pulled desktop junk, not the Hidden ``.otp`` master). Do **not** use
``.uno:DiaMode`` Copy/Paste or ``.uno:PresentationLayout`` PropertyValues
(SDI formal args empty → silent no-op). Cross-doc ``MasterPage`` assign is
a no-op / unsafe. ``XTransferableSupplier`` is not reachable on Impress
controllers from PyUNO.

Working path: Hidden-open the listed ``.otp``, **clone** its master into
the open deck (``createInstance`` + ``add`` + set Size/Position **after**
add so placeholder geometry matches; GraphicObjectShape via ``Graphic``),
then ``page.MasterPage = cloned_master`` on every slide (Exchange-all).
Style-family copies share the hidden source item pool and abort soffice,
so they are not part of this path.

``apply_design`` always restyles the **open** Impress deck this way. A new
window would spawn a second sidebar agent with empty chat context — do not
reintroduce a model-facing create-from-template tool. Start from a blank
deck or File→Templates, then ``list_designs`` → ``apply_design``. The result
includes ``source_master`` and ``selection``; a fallback warning names the
master when several untitled masters tie.

``list_designs`` includes a short ``look`` string (mood / accent hues /
illustrated vs graphic chrome) derived from the ``.otp`` ZIP — see
``design_look.derive_otp_look``. ZIP member reading and extraction are
handled via ``otp_zip``. No Desktop open, no vision images.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from typing import Any, NamedTuple

from plugin.doc.doc_type import is_impress
from plugin.doc.document_research import _path_settings_from_ctx, _resolve_lo_directory_path, _should_skip_filename
from plugin.draw.base import DRAW_AND_IMPRESS
from plugin.draw.design_look import derive_otp_look
from plugin.draw.masters import find_master, iter_masters
from plugin.draw.otp_zip import extract_member, read_member
from plugin.draw.shape_clone import clone_shape, copy_prop, fill_style_kind
from plugin.framework.tool import ToolBase, ToolContext
from plugin.framework.errors import is_real_disposal
from plugin.framework.uno_context import uno_same
from plugin.framework.url_utils import path_to_file_url

log = logging.getLogger("writeragent.draw.designs")

# Distinct from create-from-template's ``_wa_impress_otp`` so a hidden source
# load does not collide with a leftover new-doc Hidden frame.
_IMPORT_SOURCE_TARGET = "_wa_impress_otp_import"


class ClonedMaster(NamedTuple):
    master: Any
    name: str
    shape_count: int
    graphic_shapes: int = 0
    graphic_reimports: int = 0
    warnings: tuple[str, ...] = ()
    source_master: str = ""
    selection: str = ""


class DesignChromeMissingError(RuntimeError):
    def __init__(
        self,
        message: str,
        graphic_shapes: int = 0,
        graphic_reimports: int = 0,
        warnings: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.graphic_shapes: int = graphic_shapes
        self.graphic_reimports: int = graphic_reimports
        self.warnings: tuple[str, ...] = tuple(warnings)


def _decode_odf_name(name: str) -> str:
    """Decode ODF style name escapes (_20_ -> space, _5f_ -> _, _3a_ -> :)."""
    return re.sub(r"_([0-9a-fA-F]{2})_", lambda m: chr(int(m.group(1), 16)), name)


def _normalize_master_name(name: str) -> str:
    """Normalize master name: decode ODF, lower, strip ratio suffix, and clean spaces."""
    s = _decode_odf_name(str(name or "")).lower()
    s = re.sub(r"(?:^|[\s_-]+)(?:16[:_x -]?9|4[:_x -]?3)\s*$", "", s)
    s = re.sub(r"[_-]+", " ", s)
    return " ".join(s.split())


def _master_image_hrefs_from_zip(zf: Any, master_name: str) -> list[str] | None:
    """Read styles.xml and collect draw:image/@xlink:href for the named master page."""
    import xml.etree.ElementTree as ET  # nosemgrep: use-defused-xml  # shipped .otp styles.xml, size-capped by read_member

    raw_xml = read_member(zf, "styles.xml")
    if raw_xml is None:
        return None
    raw_upper = raw_xml.upper()
    if b"<!DOCTYPE" in raw_upper or b"<!ENTITY" in raw_upper:
        return None
    try:
        root = ET.fromstring(raw_xml)
    except Exception:
        return None
    for elem in root.iter():
        if elem.tag.endswith("master-page"):
            raw_name = (
                elem.attrib.get("{urn:oasis:names:tc:opendocument:xmlns:style:1.0}name")
                or elem.attrib.get("style:name")
                or elem.attrib.get("name")
            )
            if raw_name:
                decoded = _decode_odf_name(raw_name)
                if decoded == master_name or decoded.strip().lower() == master_name.strip().lower():
                    hrefs: list[str] = []
                    for desc in elem.iter():
                        if desc.tag.endswith("image") and not desc.tag.endswith("fill-image"):
                            href = (
                                desc.attrib.get("{http://www.w3.org/1999/xlink}href")
                                or desc.attrib.get("xlink:href")
                                or desc.attrib.get("href")
                            )
                            if href:
                                hrefs.append(href)
                    return hrefs
    return None

_CLONE_MASTER_PAGE_PROPS = ("BackgroundFullSize", "BorderLeft", "BorderRight", "BorderTop", "BorderBottom", "Width", "Height")

# PathSettings properties that hold template directories. Discover extras whose
# name contains "emplate" so we do not hardcode an install prefix.
_TEMPLATE_PROP_FALLBACK = ("Template", "Template_internal", "Template_user", "Template_writable")

_OTP_EXT = ".otp"


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
            # Encoded spaces are %20; a raw space after file: starts a new URL.
            if token.startswith("file:"):
                if buf:
                    parts.append(buf)
                buf = token
            elif buf:
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


def enumerate_impress_designs(ctx: Any, *, include_look: bool = True) -> list[dict[str, str]]:
    """Walk PathSettings template dirs for ``.otp`` files. Stable id = stem lowercased."""
    designs_by_id: dict[str, dict[str, str]] = {}
    limit = 1000
    for directory in _iter_template_directories(ctx):
        for root, dirnames, files in os.walk(directory):
            dirnames.sort()
            files.sort()
            for filename in files:
                if _should_skip_filename(filename):
                    continue
                if not filename.lower().endswith(_OTP_EXT):
                    continue
                full = os.path.normpath(os.path.join(root, filename))
                if not os.path.isfile(full):
                    continue
                stem = os.path.splitext(os.path.basename(full))[0]
                did = stem.strip().lower()
                if len(designs_by_id) >= limit and did not in designs_by_id:
                    out = list(designs_by_id.values())
                    out.sort(key=lambda d: (d["name"].lower(), d["path"]))
                    return out
                entry = {
                    "id": did,
                    "name": stem,
                    "path": full,
                    "url": path_to_file_url(full),
                    # ZIP thumbnail / Pictures / styles — never Desktop-open.
                    # Name/path match does not need this. Opening every .otp
                    # just to resolve a design name made apply_design scan
                    # the template tree twice on a miss.
                    "look": derive_otp_look(full) if include_look else "",
                }
                designs_by_id[did] = entry
    out = list(designs_by_id.values())
    out.sort(key=lambda d: (d["name"].lower(), d["path"]))
    return out


def _match_listed_design(designs: list[dict[str, str]], needle: str) -> dict[str, str] | None:
    """Match a listed design by id, name, path, or file URL."""
    lower = needle.lower()
    lower_stem = lower[:-4] if lower.endswith(".otp") else lower
    path_norm = os.path.normcase(os.path.normpath(needle)) if not needle.startswith("file:") else ""
    for entry in designs:
        if entry["id"] == lower or entry["name"].lower() == lower:
            return entry
        if lower_stem != lower and (entry["id"] == lower_stem or entry["name"].lower() == lower_stem):
            return entry
        if path_norm and os.path.normcase(entry["path"]) == path_norm:
            return entry
        if needle.startswith("file:") and entry["url"] == needle:
            return entry
    return None


def resolve_design(ctx: Any, design: str) -> dict[str, str] | None:
    """Match a listed design by id, name, path, or file URL."""
    needle = str(design or "").strip()
    if not needle:
        return None
    return _match_listed_design(enumerate_impress_designs(ctx, include_look=False), needle)


def _master_entries(doc: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, m, name in iter_masters(doc):
        shapes = int(m.getCount())
        out.append({"index": i, "name": name, "shape_count": shapes})
    return out


def _blank_master_signal(masters: list[dict[str, Any]]) -> bool:
    """True when the deck looks like factory Default (white / empty-ish master)."""
    if not masters:
        return True
    names = [str(m.get("name") or "").strip().lower() for m in masters]
    if any(n and n != "default" for n in names):
        return False
    return all(int(m.get("shape_count") or 0) < 3 for m in masters)


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


def _load_design_model(
    uno_ctx: Any,
    design: dict[str, str],
    target: str,
    *,
    hidden: bool,
    as_template: bool,
) -> Any:
    from plugin.framework.uno_context import get_desktop
    from plugin.writer.format import create_property_value

    desktop = get_desktop(uno_ctx)
    if desktop is None:
        raise RuntimeError("LibreOffice desktop is not available.")
    url = design.get("url") or path_to_file_url(design["path"])
    props = []
    if hidden:
        props.append(create_property_value("Hidden", True))
    if as_template:
        props.append(create_property_value("AsTemplate", True))
    model = desktop.loadComponentFromURL(url, target, 0, tuple(props))
    if model is None:
        raise RuntimeError("loadComponentFromURL returned no document for %s" % url)
    return model


def open_design_source_hidden(uno_ctx: Any, design: dict[str, str], *, as_template: bool = True) -> Any:
    """Hidden-open a listed ``.otp`` as the master-clone source.

    AsTemplate=True matches the known-good new-doc control (full Metropolis
    master + SVG chrome). Distinct target from ``create_presentation_from_design``.
    """
    from plugin.framework.thread_guard import guard_uno

    model = _load_design_model(uno_ctx, design, _IMPORT_SOURCE_TARGET, hidden=True, as_template=as_template)
    try:
        return guard_uno(model)
    except Exception:
        _close_hidden_doc(model)
        raise


def _clear_master_shapes(master: Any) -> int:
    """Remove factory placeholders so the clone is only source shapes."""
    removed = 0
    try:
        count = int(master.getCount())
    except Exception as exc:
        if is_real_disposal(exc):
            raise
        return 0
    for i in range(count - 1, -1, -1):
        try:
            master.remove(master.getByIndex(i))
            removed += 1
        except Exception as exc:
            if is_real_disposal(exc):
                raise
            log.debug("clear master shape failed index=%s", i, exc_info=True)
    return removed


def _clone_master_background(dest_master: Any, src_master: Any, warnings: list[str]) -> None:
    """Reconstruct page background without assigning source Background object."""
    try:
        src_bg = getattr(src_master, "Background", None)
        if src_bg is None and hasattr(src_master, "getPropertyValue"):
            src_bg = src_master.getPropertyValue("Background")
    except Exception as exc:
        if is_real_disposal(exc):
            raise
        src_bg = None
    if src_bg is None:
        return
    try:
        fill_style = (
            src_bg.getPropertyValue("FillStyle")
            if hasattr(src_bg, "getPropertyValue")
            else getattr(src_bg, "FillStyle", None)
        )
    except Exception as exc:
        if is_real_disposal(exc):
            raise
        fill_style = getattr(src_bg, "FillStyle", None)
    kind = fill_style_kind(fill_style)
    if kind == "none":
        return

    try:
        dest_bg = getattr(dest_master, "Background", None)
        if dest_bg is None and hasattr(dest_master, "getPropertyValue"):
            dest_bg = dest_master.getPropertyValue("Background")
    except Exception as exc:
        if is_real_disposal(exc):
            raise
        dest_bg = None

    if dest_bg is None:
        warnings.append(f"fill {kind} not copied on master")
        return

    if kind == "solid":
        copy_prop(dest_bg, src_bg, "FillStyle")
        if copy_prop(dest_bg, src_bg, "FillColor") != "copied":
            warnings.append("fill solid not copied on master")
    elif kind == "gradient":
        copy_prop(dest_bg, src_bg, "FillStyle")
        if copy_prop(dest_bg, src_bg, "FillGradient") != "copied":
            warnings.append("fill gradient not copied on master")
        else:
            copy_prop(dest_bg, src_bg, "FillTransparenceGradient")
    elif kind == "hatch":
        copy_prop(dest_bg, src_bg, "FillStyle")
        if copy_prop(dest_bg, src_bg, "FillHatch") != "copied":
            warnings.append("fill hatch not copied on master")
    elif kind == "bitmap":
        warnings.append("fill bitmap not copied on master")
    else:
        warnings.append(f"fill {kind} not copied on master")


# Zip hrefs are document order and include draw:g children. A top-level-only
# count fails the equality check, extraction is skipped, and every picture uses storeGraphic.
def _count_graphic_shapes(container: Any) -> int:
    try:
        total = 0
        for i in range(int(container.getCount())):
            shape = container.getByIndex(i)
            st = str(getattr(shape, "ShapeType", "") or "")
            if "GraphicObject" in st:
                total += 1
            elif "GroupShape" in st:
                total += _count_graphic_shapes(shape)
        return total
    except Exception as exc:
        if is_real_disposal(exc):
            raise
        return 0


def clone_master_into_doc(dest_doc: Any, src_doc: Any, design: dict[str, str], uno_ctx: Any = None) -> ClonedMaster:
    """Clone the source design master into *dest_doc* (same-document shapes).

    Cross-doc ``MasterPage`` assign does not import. ``createInstance`` +
    ``add`` of a GraphicObjectShape works; adding the foreign shape object
    raises IllegalArgumentException (load-master probe).
    """
    src_masters = src_doc.getMasterPages()
    if src_masters.getCount() < 1:
        raise RuntimeError("Design source has no master pages.")

    design_name = str(design.get("name") or "").strip()
    design_id = str(design.get("id") or "").strip().lower()

    dest_ratio = 16.0 / 9.0
    dw = 0
    dh = 0
    try:
        pages = dest_doc.getDrawPages()
        p0 = pages.getByIndex(0) if pages.getCount() > 0 else None
        if p0 is not None:
            dw = getattr(p0, "Width", 0) or 0
            dh = getattr(p0, "Height", 0) or 0
            if (not dw or not dh) and hasattr(p0, "MasterPage") and p0.MasterPage:
                dw = getattr(p0.MasterPage, "Width", 0) or 0
                dh = getattr(p0.MasterPage, "Height", 0) or 0
        if not dw or not dh:
            m0 = find_master(dest_doc, index=0)
            if m0 is not None:
                dw = getattr(m0, "Width", 0) or 0
                dh = getattr(m0, "Height", 0) or 0
        if dw and dh and float(dh) > 0:
            dest_ratio = float(dw) / float(dh)
    except Exception as exc:
        if is_real_disposal(exc):
            raise

    norm_design_id = _normalize_master_name(design_id)
    norm_design_name = _normalize_master_name(design_name)

    matching_candidates: list[tuple[int, Any, float]] = []
    all_candidates: list[tuple[int, Any, float]] = []

    for i in range(src_masters.getCount()):
        try:
            cand = src_masters.getByIndex(i)
        except Exception as exc:
            if is_real_disposal(exc):
                raise
            continue
        try:
            cand_name = str(getattr(cand, "Name", "") or "")
        except Exception as exc:
            if is_real_disposal(exc):
                raise
            cand_name = ""
        norm_cand_name = _normalize_master_name(cand_name)

        # Shipped .otp masters often report 16:9 geometry even when named 4:3; prefer named ratio.
        cand_ratio = 0.0
        if re.search(r"4[:_x -]?3", cand_name) or re.search(r"_20_4_3a_3", cand_name):
            cand_ratio = 4.0 / 3.0
        elif re.search(r"16[:_x -]?9", cand_name) or re.search(r"_20_16_3a_9", cand_name):
            cand_ratio = 16.0 / 9.0
        else:
            try:
                sw = getattr(cand, "Width", 0) or 0
                sh = getattr(cand, "Height", 0) or 0
                if sw and sh and float(sh) > 0:
                    cand_ratio = float(sw) / float(sh)
            except Exception as exc:
                if is_real_disposal(exc):
                    raise
                cand_ratio = 0.0
            if not cand_ratio:
                cand_ratio = dest_ratio
        diff = abs(cand_ratio - dest_ratio)

        item = (i, cand, diff)
        all_candidates.append(item)
        if norm_cand_name and (norm_cand_name == norm_design_id or norm_cand_name == norm_design_name):
            matching_candidates.append(item)

    pool = matching_candidates if matching_candidates else all_candidates
    _best_idx, best_master, best_diff = min(pool, key=lambda x: (x[2], x[0]))
    src_master = best_master
    source_master_name = str(getattr(src_master, "Name", "") or "")
    aspect_mismatch = best_diff > 0.05

    if matching_candidates:
        selection = "name"
    else:
        min_diff = min(c[2] for c in all_candidates)
        tied_count = sum(1 for c in all_candidates if abs(c[2] - min_diff) < 1e-6)
        selection = "ratio" if tied_count == 1 else "fallback"

    if not design_name:
        design_name = source_master_name or "Imported"

    dest_masters = dest_doc.getMasterPages()
    # Always insert a new master. Reusing a same-named master cleared its
    # shapes before the copy, so a later failure left that master empty.
    dest_master = dest_masters.insertNewByIndex(dest_masters.getCount())
    if dest_master is None:
        raise RuntimeError("Could not insert a destination master page.")

    tmp_dir = tempfile.mkdtemp(prefix="wa_otp_clone_")
    try:
        try:
            dest_master.Name = design_name
        except Exception as exc:
            if is_real_disposal(exc):
                raise
            log.debug("rename cloned master to %s failed", design_name, exc_info=True)

        warnings: list[str] = []
        if selection == "fallback":
            warnings.append(f"no master name matched; used '{source_master_name}'")
        if aspect_mismatch:
            warnings.append("aspect mismatch")
        # Factory placeholders on the new master are not user content.
        _clear_master_shapes(dest_master)
        for prop in _CLONE_MASTER_PAGE_PROPS:
            res = copy_prop(dest_master, src_master, prop)
            if res == "rejected":
                warnings.append(f"rejected {prop} on master")
        # Size the clone to the destination deck so named 4:3 masters with 16:9 .otp geometry still apply as 4:3.
        if dw and dh:
            try:
                dest_master.Width = int(dw)
                dest_master.Height = int(dh)
            except Exception as exc:
                if is_real_disposal(exc):
                    raise
                log.debug("set cloned master size to dest deck failed", exc_info=True)
        _clone_master_background(dest_master, src_master, warnings)
        try:
            src_count = int(src_master.getCount())
        except Exception as exc:
            if is_real_disposal(exc):
                raise
            src_count = 0

        graphic_shape_count = _count_graphic_shapes(src_master)

        otp_path = str(design.get("path") or "").strip() or None
        extracted_image_paths: list[str | None] = []
        if otp_path and os.path.isfile(otp_path):
            import zipfile

            try:
                with zipfile.ZipFile(otp_path, "r") as zf:
                    hrefs = _master_image_hrefs_from_zip(zf, str(src_master.Name or ""))
                    pics = [n for n in zf.namelist() if n.replace("\\", "/").lstrip("./").lower().startswith("pictures/") and not n.endswith("/")]
                    if hrefs is not None:
                        if len(hrefs) == graphic_shape_count:
                            for idx, href in enumerate(hrefs):
                                path = extract_member(zf, href, tmp_dir, prefix=f"{idx}_")
                                extracted_image_paths.append(path)
                    elif len(pics) == 1 and graphic_shape_count == 1:
                        path = extract_member(zf, pics[0], tmp_dir, prefix="0_")
                        extracted_image_paths.append(path)
            except Exception:
                log.debug("reading otp zip failed for %s", otp_path, exc_info=True)

        cloned_types: list[str] = []
        graphic_shapes = 0
        graphic_reimports = 0
        image_paths = extracted_image_paths if extracted_image_paths else None
        for i in range(src_count):
            src_shape = src_master.getByIndex(i)
            st = str(getattr(src_shape, "ShapeType", "") or "")
            log.debug("clone_master step=shape i=%s/%s type=%s", i, src_count, st)
            result = clone_shape(
                dest_doc,
                dest_master,
                src_shape,
                uno_ctx=uno_ctx,
                image_paths=image_paths,
                shape_index=i,
                warnings=warnings,
            )
            if result.empty_group_message:
                raise DesignChromeMissingError(result.empty_group_message, warnings=tuple(warnings))
            cloned_types.append(result.shape_type)
            graphic_shapes += result.graphic_shapes
            graphic_reimports += result.graphic_reimports
            log.debug("clone_master step=shape_done i=%s type=%s", i, result.shape_type)

        dest_name = str(dest_master.Name or "") or design_name
        # Do not copy the presentation style family here. Blind style-prop copies
        # share the Hidden source SfxItemPool and abort soffice on source close
        # (GetUserOrPoolDefaultItem). Shape primitives + reimported Graphic are
        # enough for Metropolis chrome (title geom 14800 + SVG).
        # MasterPage.getCount() does not fail on a live page.
        shape_count = int(dest_master.getCount())
        if shape_count < 1:
            raise RuntimeError("Cloned master '%s' has no shapes." % dest_name)

        if graphic_shapes >= 1 and graphic_reimports == 0:
            raise DesignChromeMissingError(
                "No chrome graphics could be imported for master '%s'." % dest_name,
                graphic_shapes=graphic_shapes,
                graphic_reimports=0,
                warnings=tuple(warnings),
            )

        log.debug("clone_master_into_doc name=%s shapes=%s types=%s warnings=%s", dest_name, shape_count, cloned_types, warnings)
        return ClonedMaster(
            dest_master,
            dest_name,
            shape_count,
            graphic_shapes,
            graphic_reimports,
            tuple(warnings),
            source_master=source_master_name,
            selection=selection,
        )
    except Exception:
        try:
            dest_masters.remove(dest_master)
        except Exception as remove_exc:
            if is_real_disposal(remove_exc):
                raise
            log.debug("remove partial cloned master failed", exc_info=True)
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def assign_master_to_all_slides(doc: Any, master: Any) -> int:
    """Exchange-background-page equivalent: assign *master* to every draw page."""
    pages = doc.getDrawPages()
    updated = 0
    for i in range(pages.getCount()):
        try:
            pages.getByIndex(i).MasterPage = master
            updated += 1
        except Exception as exc:
            if is_real_disposal(exc):
                raise
            log.debug("assign_master_to_all_slides failed on page %s", i, exc_info=True)
    return updated


def _is_master_referenced(doc: Any, master: Any) -> bool:
    """True if any draw page still references *master*."""
    try:
        pages = doc.getDrawPages()
        for i in range(pages.getCount()):
            if uno_same(pages.getByIndex(i).MasterPage, master):
                return True
    except Exception as exc:
        if is_real_disposal(exc):
            raise
        log.debug("error checking master reference; assuming referenced", exc_info=True)
        return True
    return False


def _restore_slide_masters(snapshot: list[tuple[Any, Any]], *, swallow_disposal: bool = False) -> int:
    """Restore previous MasterPage for each page in snapshot."""
    restored = 0
    for page, old_master in snapshot:
        try:
            page.MasterPage = old_master
            restored += 1
        except Exception as exc:
            if not swallow_disposal and is_real_disposal(exc):
                raise
            log.debug("restore master page failed", exc_info=True)
    return restored


def _cleanup_previous_same_named_masters(dest_doc: Any, new_master: Any, design_name: str) -> None:
    """Remove unreferenced masters matching design_name (case-insensitive), preserving Default."""
    if not design_name:
        return
    target_lower = design_name.strip().lower()
    if target_lower == "default":
        return
    to_remove: list[Any] = []
    for _i, m, name in iter_masters(dest_doc):
        if uno_same(m, new_master):
            continue
        if name.strip().lower() == target_lower:
            if not _is_master_referenced(dest_doc, m):
                to_remove.append(m)
    for m in to_remove:
        try:
            dest_doc.getMasterPages().remove(m)
        except Exception as exc:
            if is_real_disposal(exc):
                raise
            log.debug("remove old same-named master failed", exc_info=True)


def apply_design_to_current_doc(uno_ctx: Any, dest_doc: Any, design: dict[str, str]) -> dict[str, Any]:
    """Import a shipped ``.otp`` master into *dest_doc* and assign it to all slides."""
    # getDrawPages().getCount() does not fail on a live document.
    # A closed document must not be reported as zero slides.
    try:
        dest_doc.getDrawPages().getCount()
    except Exception as exc:
        if is_real_disposal(exc):
            raise
    try:
        is_ro = bool(callable(getattr(dest_doc, "isReadonly", None)) and dest_doc.isReadonly() is True)
    except Exception as ro_exc:
        if is_real_disposal(ro_exc):
            raise
        is_ro = False
    if is_ro:
        return {
            "status": "error",
            "code": "DOCUMENT_READONLY",
            "message": "Presentation document is read-only.",
        }
    src = None
    cloned: ClonedMaster | None = None
    master_name = ""
    slides_updated = 0
    clone_warnings_list: list[str] = []
    design_label = design.get("name") or design.get("id") or "?"
    target_design_name = str(design.get("name") or design_label).strip()

    try:
        src = open_design_source_hidden(uno_ctx, design, as_template=True)
        cloned = clone_master_into_doc(dest_doc, src, design, uno_ctx)
        master_name = cloned.name
        clone_warnings_list = list(cloned.warnings)

        # Snapshot each page's MasterPage before assign
        snapshot: list[tuple[Any, Any]] = []
        try:
            pages = dest_doc.getDrawPages()
            for i in range(pages.getCount()):
                p = pages.getByIndex(i)
                snapshot.append((p, getattr(p, "MasterPage", None)))

            slides_updated = assign_master_to_all_slides(dest_doc, cloned.master)
            slide_count = int(dest_doc.getDrawPages().getCount())
            if slides_updated != slide_count:
                raise RuntimeError("Partial slide assignment")

            # Replace previous unreferenced master of the same design name
            _cleanup_previous_same_named_masters(dest_doc, cloned.master, target_design_name)
            try:
                cloned.master.Name = target_design_name
                master_name = target_design_name
            except Exception as exc:
                if is_real_disposal(exc):
                    raise
                master_name = str(getattr(cloned.master, "Name", "") or "") or target_design_name
        except Exception as post_exc:
            if is_real_disposal(post_exc):
                _restore_slide_masters(snapshot, swallow_disposal=True)
                raise
            slides_restored = _restore_slide_masters(snapshot)
            if not _is_master_referenced(dest_doc, cloned.master):
                try:
                    dest_doc.getMasterPages().remove(cloned.master)
                except Exception as rem_exc:
                    if is_real_disposal(rem_exc):
                        raise
                    log.debug("remove rolled-back master failed", exc_info=True)
            try:
                slide_count = int(dest_doc.getDrawPages().getCount())
            except Exception:
                slide_count = len(snapshot)
            log.info(
                "apply_design partial assign rolled back design=%s updated=%s/%s restored=%s",
                design_label,
                slides_updated,
                slide_count,
                slides_restored,
            )
            return {
                "status": "error",
                "code": "DESIGN_PARTIAL_APPLY",
                "message": "Rolled back after applying '%s' to %s of %s slides." % (design.get("name"), slides_updated, slide_count),
                "design": design,
                "applied_master": master_name,
                "source_master": cloned.source_master,
                "selection": cloned.selection,
                "applied_master_shape_count": cloned.shape_count,
                "masters": _master_entries(dest_doc),
                "slides_updated": slides_updated,
                "slide_count": slide_count,
                "rolled_back": True,
                "slides_restored": slides_restored,
                "import_method": "clone_master",
                "clone_warnings": clone_warnings_list[:12],
                "clone_warning_count": len(clone_warnings_list),
            }

        log.info(
            "apply_design design=%s master=%s shapes=%s graphics=%s/%s slides=%s",
            design_label,
            master_name,
            cloned.shape_count,
            cloned.graphic_reimports,
            cloned.graphic_shapes,
            slides_updated,
        )
    except DesignChromeMissingError as exc:
        log.warning("apply_design failed chrome missing: %s", exc)
        warns = list(exc.warnings)
        return {
            "status": "error",
            "code": "DESIGN_CHROME_MISSING",
            "message": str(exc),
            "design": design,
            "graphic_shapes": exc.graphic_shapes,
            "graphic_reimports": exc.graphic_reimports,
            "clone_warnings": warns[:12],
            "clone_warning_count": len(warns),
        }
    finally:
        _close_hidden_doc(src)

    assert cloned is not None
    masters = _master_entries(dest_doc)
    slide_count = int(dest_doc.getDrawPages().getCount())
    if cloned.graphic_shapes > 0 and cloned.graphic_reimports < cloned.graphic_shapes:
        msg = (
            "Applied design '%s' (master '%s') to the current presentation (%s of %s chrome shapes imported)."
            % (design.get("name"), master_name, cloned.graphic_reimports, cloned.graphic_shapes)
        )
    else:
        msg = "Applied design '%s' (master '%s') to the current presentation." % (design.get("name"), master_name)
    return {
        "status": "ok",
        "design": design,
        "applied_master": master_name,
        "source_master": cloned.source_master,
        "selection": cloned.selection,
        "applied_master_shape_count": cloned.shape_count,
        "graphic_shapes": cloned.graphic_shapes,
        "graphic_reimports": cloned.graphic_reimports,
        "masters": masters,
        "blank_master": _blank_master_signal(masters),
        "slides_updated": slides_updated,
        "import_method": "clone_master",
        "slide_count": slide_count,
        "message": msg,
        "clone_warnings": clone_warnings_list[:12],
        "clone_warning_count": len(clone_warnings_list),
    }


def create_presentation_from_design(uno_ctx: Any, design: dict[str, str], *, hidden: bool = False) -> Any:
    """``loadComponentFromURL`` + ``AsTemplate`` — new document (M0′ proven path).

    Internal / test helper only. Not model-facing: a new Impress window
    gets a fresh sidebar agent and drops mid-chat context.
    """
    from plugin.framework.thread_guard import guard_uno

    # Unique CREATE|GLOBAL target for Hidden loads (leftover ``_blank`` hangs on
    # some Windows GHA runs). Visible product opens stay ``_blank``.
    target = "_wa_impress_otp" if hidden else "_blank"
    model = _load_design_model(uno_ctx, design, target, hidden=hidden, as_template=True)
    return guard_uno(model)


class ListDesigns(ToolBase):
    """Enumerate shipped Impress ``.otp`` designs via PathSettings."""

    name: str | None = "list_designs"
    description: str = (
        "List shipped Impress .otp designs from LibreOffice PathSettings template "
        "directories (id, name, path, url, look). Read look to choose a design by "
        "appearance (dark/tech vs candy/illustrated), not only by name. Use a listed "
        "id with apply_design to restyle the open Impress deck. Does not hardcode the "
        "install prefix."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}
    uno_services: list[str] | None = DRAW_AND_IMPRESS
    tier: str = "core"
    is_mutation: bool | None = False

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        designs = enumerate_impress_designs(ctx.ctx)
        return {"status": "ok", "designs": designs, "count": len(designs)}


class ApplyDesign(ToolBase):
    """Restyle the open Impress deck from a listed ``.otp`` (clone_master)."""

    name: str | None = "apply_design"
    description: str = (
        "Restyle the OPEN Impress deck from a listed .otp: Hidden-load the "
        "template, clone its master shapes and reimported graphics into this "
        "document, and assign that master to every slide. Does not use "
        "the system clipboard and does not open a new presentation. "
        "The master matching the deck's aspect ratio and design name is cloned "
        "and assigned to every slide; other layout masters in the .otp are not imported. "
        "Groups, custom-shape geometry, polygons, rotation, gradient and hatch structs, "
        "solid master backgrounds, line and character properties, and pictures are cloned. "
        "Bitmap fills and presentation style families are not (copying those families shares "
        "the hidden source pool and aborts LibreOffice). "
        "The result may include clone_warnings when a property is rejected, an aspect ratio "
        "mismatch occurs, or a bitmap fill was not reproduced. "
        "The result includes source_master and selection; a fallback warning names the master "
        "when several untitled masters tie. "
        "Existing placeholder text stays on its current styles. "
        "Applying the same design again replaces the previously imported master of that name. "
        "Call list_designs first. Draw documents return a not-Impress error."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"design": {"type": "string", "description": "Design id, name, path, or url from list_designs (e.g. Metropolis)."}}, "required": ["design"]}
    uno_services: list[str] | None = DRAW_AND_IMPRESS
    tier: str = "core"
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        # Draw and every other non-Impress doc share this error.
        # _is_draw_only_doc returned the same payload.
        if not is_impress(ctx.doc):
            return self._tool_error(
                "apply_design requires an Impress presentation, not a Draw document.",
                code="UNSUPPORTED_DOC_TYPE",
                doc_type="draw",
            )
        design_key = kwargs["design"].strip()
        if not design_key:
            return self._tool_error("design is required.")
        # One walk, no ZIP look. resolve_design used to open every .otp,
        # and a miss enumerated the tree again.
        listed = enumerate_impress_designs(ctx.ctx, include_look=False)
        entry = _match_listed_design(listed, design_key)
        if entry is None:
            available = [d["id"] for d in listed]
            return self._tool_error("Design not found: %s" % design_key, available=available[:40])
        try:
            return apply_design_to_current_doc(ctx.ctx, ctx.doc, entry)
        except Exception as e:
            if is_real_disposal(e):
                raise
            log.exception("current-doc apply_design failed for %s", entry.get("path"))
            return self._tool_error("Failed to apply design to the current presentation: %s" % e, reason="current_doc_import_failed", hint="Hidden .otp master clone or MasterPage assign did not complete.")

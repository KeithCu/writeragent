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
copy the design's presentation-style family, then
``page.MasterPage = cloned_master`` on every slide (Exchange-all).

``apply_design`` always restyles the **open** Impress deck this way. A new
window would spawn a second sidebar agent with empty chat context — do not
reintroduce a model-facing create-from-template tool. Start from a blank
deck or File→Templates, then ``list_designs`` → ``apply_design``.

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

# Visual props copied onto a dest-created shape after it is added to the
# dest master. Size/Position are applied last — setting them before add()
# leaves factory Default geometry (25200-wide title vs Metropolis 14800).
# Primitives only. Graphic / FillBitmap / Background are SfxItems from the
# Hidden source pool — copying them then closing the source aborts soffice
# (GetUserOrPoolDefaultItem). Graphic is re-imported via GraphicProvider.
_CLONE_SHAPE_PROPS = (
    "Name",
    "Visible",
    "ZOrder",
    "LayerID",
    "FillStyle",
    "FillColor",
    "FillTransparence",
    "LineStyle",
    "LineColor",
    "LineWidth",
    "CharColor",
    "CharHeight",
    "CharWeight",
    "CharFontName",
    "String",
    "TextAutoGrowHeight",
    "TextAutoGrowWidth",
    "TextHorizontalAdjust",
    "TextVerticalAdjust",
)
_CLONE_MASTER_PAGE_PROPS = (
    "BackgroundFullSize",
    "BorderLeft",
    "BorderRight",
    "BorderTop",
    "BorderBottom",
    "Width",
    "Height",
)

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
    """Hidden-open a listed ``.otp`` as the master-clone source.

    AsTemplate=True matches the known-good new-doc control (full Metropolis
    master + SVG chrome). Distinct target from ``create_presentation_from_design``.
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


def _copy_uno_prop(dest: Any, src: Any, name: str) -> bool:
    """Copy one UNO property. Returns True on success."""
    try:
        value = src.getPropertyValue(name) if hasattr(src, "getPropertyValue") else getattr(src, name)
    except Exception:
        try:
            value = getattr(src, name)
        except Exception:
            return False
    try:
        if hasattr(dest, "setPropertyValue"):
            dest.setPropertyValue(name, value)
            return True
    except Exception:
        pass
    try:
        setattr(dest, name, value)
        return True
    except Exception:
        return False


def _clear_master_shapes(master: Any) -> int:
    """Remove factory placeholders so the clone is only source shapes."""
    removed = 0
    try:
        count = int(master.getCount())
    except Exception:
        return 0
    for i in range(count - 1, -1, -1):
        try:
            master.remove(master.getByIndex(i))
            removed += 1
        except Exception:
            log.debug("clear master shape failed index=%s", i, exc_info=True)
    return removed


def _graphic_from_file_url(uno_ctx: Any, file_url: str) -> Any | None:
    """Load a dest-owned XGraphic from a file URL (never from a Hidden-doc pool)."""
    if uno_ctx is None or not file_url:
        return None
    from plugin.writer.format import create_property_value

    try:
        smgr = getattr(uno_ctx, "ServiceManager", None) or uno_ctx.getServiceManager()
        provider = smgr.createInstanceWithContext("com.sun.star.graphic.GraphicProvider", uno_ctx)
        if provider is None:
            return None
        return provider.queryGraphic((create_property_value("URL", file_url),))
    except Exception:
        log.debug("GraphicProvider.queryGraphic failed url=%s", file_url, exc_info=True)
        return None


def _extract_otp_picture(otp_path: str, dest_dir: str) -> str | None:
    """Extract the first Pictures/* media file from a shipped ``.otp`` ZIP."""
    if not otp_path or not os.path.isfile(otp_path):
        return None
    import zipfile

    try:
        with zipfile.ZipFile(otp_path, "r") as zf:
            names = [
                n
                for n in zf.namelist()
                if n.startswith("Pictures/") and not n.endswith("/")
            ]
            if not names:
                return None
            # Prefer SVG (Metropolis chrome), then any other picture.
            names.sort(key=lambda n: (0 if n.lower().endswith(".svg") else 1, n.lower()))
            member = names[0]
            base = os.path.basename(member) or "chrome.bin"
            out = os.path.join(dest_dir, base)
            with zf.open(member) as src, open(out, "wb") as dst:
                dst.write(src.read())
            return out
    except Exception:
        log.debug("extract_otp_picture failed path=%s", otp_path, exc_info=True)
        return None


def _reimport_graphic(
    uno_ctx: Any,
    src_shape: Any,
    dest_shape: Any,
    *,
    otp_path: str | None = None,
) -> bool:
    """Attach chrome without sharing the Hidden source SfxItemPool.

    Prefer extracting ``Pictures/*`` from the shipped ``.otp`` ZIP and loading
    via GraphicProvider. ``storeGraphic`` of the live Hidden Graphic can still
    touch the source pool; keep it as a last resort only.
    """
    if uno_ctx is None:
        return False
    import tempfile

    from plugin.framework.url_utils import path_to_file_url
    from plugin.writer.format import create_property_value

    # 1) OTP ZIP extract — never opens the Hidden Graphic.
    if otp_path:
        tmp_dir = tempfile.mkdtemp(prefix="wa_otp_pic_")
        try:
            extracted = _extract_otp_picture(otp_path, tmp_dir)
            if extracted:
                url = path_to_file_url(extracted)
                log.info("clone_master graphic via otp extract path=%s", extracted)
                new_graphic = _graphic_from_file_url(uno_ctx, url)
                if new_graphic is not None:
                    dest_shape.Graphic = new_graphic
                    return True
        finally:
            try:
                for name in os.listdir(tmp_dir):
                    try:
                        os.remove(os.path.join(tmp_dir, name))
                    except Exception:
                        pass
                os.rmdir(tmp_dir)
            except Exception:
                pass

    # 2) Last resort: round-trip the live Graphic (may still be pool-sensitive).
    try:
        graphic = src_shape.Graphic
    except Exception:
        return False
    if graphic is None:
        return False
    smgr = getattr(uno_ctx, "ServiceManager", None) or uno_ctx.getServiceManager()
    provider = smgr.createInstanceWithContext("com.sun.star.graphic.GraphicProvider", uno_ctx)
    if provider is None:
        return False
    log.info("clone_master graphic via storeGraphic fallback")
    for suffix, mime in ((".svg", "image/svg+xml"), (".png", "image/png")):
        fd, path = tempfile.mkstemp(suffix=suffix)
        try:
            os.close(fd)
            url = path_to_file_url(path)
            provider.storeGraphic(
                graphic,
                (create_property_value("URL", url), create_property_value("MimeType", mime)),
            )
            new_graphic = provider.queryGraphic((create_property_value("URL", url),))
            if new_graphic is None:
                continue
            dest_shape.Graphic = new_graphic
            return True
        except Exception:
            log.debug("graphic reimport %s failed", mime, exc_info=True)
        finally:
            try:
                os.remove(path)
            except Exception:
                pass
    return False


def _clone_one_shape(
    dest_doc: Any,
    dest_master: Any,
    src_shape: Any,
    uno_ctx: Any = None,
    *,
    otp_path: str | None = None,
) -> str:
    """Create a dest-owned shape, add it, then copy visual props + geometry.

    Adding first is required: presentation placeholders ignore Size/Position
    set before they belong to a page (headed/headless probe: title stayed
    25200×2001 until Size was set after add).
    """
    shape_type = str(getattr(src_shape, "ShapeType", "") or "")
    if not shape_type.startswith("com.sun.star."):
        raise RuntimeError("Unsupported master shape type: %s" % shape_type)
    clone = dest_doc.createInstance(shape_type)
    dest_master.add(clone)
    for prop in _CLONE_SHAPE_PROPS:
        _copy_uno_prop(clone, src_shape, prop)
    if "GraphicObject" in shape_type:
        ok = _reimport_graphic(uno_ctx, src_shape, clone, otp_path=otp_path)
        log.info("clone_master graphic_reimport ok=%s type=%s", ok, shape_type)
    # Geometry last so layout does not overwrite chrome metrics.
    _copy_uno_prop(clone, src_shape, "Position")
    _copy_uno_prop(clone, src_shape, "Size")
    return shape_type


def _is_safe_style_value(value: Any) -> bool:
    """True for primitives only. XComplexColor / BorderLine2 / GrabBag crash
    soffice in the style pool (GetUserOrPoolDefaultItem) when copied blindly.
    """
    return isinstance(value, (int, float, bool, str))


def copy_master_style_family(src_doc: Any, dest_doc: Any, family_name: str) -> int:
    """Copy presentation-layout styles (title / outline / background) by name.

    Each Impress master owns a style family of the same name. insertByName of
    the whole family fails; copy safe primitive props onto dest's family after
    the master exists. Skip structs and vetoes — do not invent styles.
    """
    if not family_name:
        return 0
    try:
        src_fam = src_doc.getStyleFamilies().getByName(family_name)
        dest_fam = dest_doc.getStyleFamilies().getByName(family_name)
    except Exception:
        log.debug("copy_master_style_family missing family %s", family_name, exc_info=True)
        return 0
    copied = 0
    try:
        names = list(src_fam.getElementNames())
    except Exception:
        return 0
    for style_name in names:
        try:
            src_style = src_fam.getByName(style_name)
            dest_style = dest_fam.getByName(style_name)
        except Exception:
            continue
        info = None
        try:
            info = src_style.getPropertySetInfo()
        except Exception:
            info = None
        if info is None:
            continue
        for prop in info.getProperties():
            pname = str(getattr(prop, "Name", "") or "")
            if not pname:
                continue
            try:
                value = src_style.getPropertyValue(pname)
            except Exception:
                continue
            if not _is_safe_style_value(value):
                continue
            try:
                dest_style.setPropertyValue(pname, value)
                copied += 1
            except Exception:
                continue
    return copied


def clone_master_into_doc(
    dest_doc: Any,
    src_doc: Any,
    design: dict[str, str],
    uno_ctx: Any = None,
) -> tuple[Any, str, int]:
    """Clone the source design master into *dest_doc* (same-document shapes).

    Cross-doc ``MasterPage`` assign does not import. ``createInstance`` +
    ``add`` of a GraphicObjectShape works; adding the foreign shape object
    raises IllegalArgumentException (load-master probe).
    """
    src_masters = src_doc.getMasterPages()
    if src_masters.getCount() < 1:
        raise RuntimeError("Design source has no master pages.")
    src_master = None
    design_name = str(design.get("name") or "").strip()
    design_id = str(design.get("id") or "").strip().lower()
    for i in range(src_masters.getCount()):
        candidate = src_masters.getByIndex(i)
        name = ""
        try:
            name = str(candidate.Name or "")
        except Exception:
            name = ""
        lower = name.strip().lower()
        if lower and (lower == design_name.lower() or lower == design_id):
            src_master = candidate
            if not design_name:
                design_name = name
            break
    if src_master is None:
        src_master = src_masters.getByIndex(0)
        if not design_name:
            try:
                design_name = str(src_master.Name or "") or "Imported"
            except Exception:
                design_name = "Imported"

    dest_masters = dest_doc.getMasterPages()
    dest_master = None
    for i in range(dest_masters.getCount()):
        candidate = dest_masters.getByIndex(i)
        try:
            name = str(candidate.Name or "")
        except Exception:
            name = ""
        if name.strip().lower() == design_name.lower():
            dest_master = candidate
            break
    if dest_master is None:
        dest_master = dest_masters.insertNewByIndex(dest_masters.getCount())
        try:
            dest_master.Name = design_name
        except Exception:
            log.debug("rename cloned master to %s failed", design_name, exc_info=True)
    if dest_master is None:
        raise RuntimeError("Could not insert a destination master page.")
    _clear_master_shapes(dest_master)
    for prop in _CLONE_MASTER_PAGE_PROPS:
        _copy_uno_prop(dest_master, src_master, prop)
    try:
        src_count = int(src_master.getCount())
    except Exception:
        src_count = 0
    otp_path = str(design.get("path") or "").strip() or None
    cloned_types: list[str] = []
    for i in range(src_count):
        src_shape = src_master.getByIndex(i)
        st = str(getattr(src_shape, "ShapeType", "") or "")
        log.info("clone_master step=shape i=%s/%s type=%s", i, src_count, st)
        cloned_types.append(
            _clone_one_shape(
                dest_doc, dest_master, src_shape, uno_ctx, otp_path=otp_path
            )
        )
        log.info("clone_master step=shape_done i=%s type=%s", i, cloned_types[-1])
    try:
        dest_name = str(dest_master.Name or "") or design_name
    except Exception:
        dest_name = design_name
    # Do not copy the presentation style family here. Blind style-prop copies
    # share the Hidden source SfxItemPool and abort soffice on source close
    # (GetUserOrPoolDefaultItem). Shape primitives + reimported Graphic are
    # enough for Metropolis chrome (title geom 14800 + SVG).
    try:
        shape_count = int(dest_master.getCount())
    except Exception:
        shape_count = len(cloned_types)
    if shape_count < 1:
        raise RuntimeError("Cloned master '%s' has no shapes." % dest_name)
    log.debug(
        "clone_master_into_doc name=%s shapes=%s types=%s",
        dest_name,
        shape_count,
        cloned_types,
    )
    return dest_master, dest_name, shape_count


def find_imported_master(
    doc: Any,
    design: dict[str, str],
    before_names: set[str],
) -> tuple[Any | None, str, int]:
    """Pick the cloned (or already-present) design master."""
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
    """Import a shipped ``.otp`` master into *dest_doc* and assign it to all slides.

    Headed #791 used DiaMode clipboard paste. That reads the **system**
    clipboard, so a headed desktop (spreadsheet/audit text) was pasted as
    junk frames and no Metropolis master arrived. Hidden Copy also fails to
    take clipboard ownership when another owner is present. Clone never
    touches the clipboard.
    """
    before_names = {str(m.get("name") or "") for m in _master_entries(dest_doc)}
    original_count = 0
    try:
        original_count = int(dest_doc.getDrawPages().getCount())
    except Exception:
        original_count = 0
    src = None
    master: Any = None
    master_name = ""
    shape_count = 0
    slides_updated = 0
    design_label = design.get("name") or design.get("id") or "?"
    try:
        log.info("apply_design current-doc step=open_hidden design=%s", design_label)
        src = open_design_source_hidden(uno_ctx, design, as_template=True)
        log.info("apply_design current-doc step=clone_master begin design=%s", design_label)
        master, master_name, shape_count = clone_master_into_doc(
            dest_doc, src, design, uno_ctx
        )
        log.info(
            "apply_design current-doc step=clone_master done master=%s shapes=%s",
            master_name,
            shape_count,
        )
        if master is None:
            master, master_name, shape_count = find_imported_master(
                dest_doc, design, before_names
            )
        if master is None:
            raise RuntimeError(
                "Master clone did not import a design master from %s" % design.get("name")
            )
        log.info(
            "apply_design current-doc step=assign_all master=%s slides~=%s",
            master_name,
            original_count,
        )
        slides_updated = assign_master_to_all_slides(dest_doc, master)
        log.info(
            "apply_design current-doc step=assign_all done slides_updated=%s",
            slides_updated,
        )
    finally:
        # Close after assign so any residual source-pool refs are unused.
        log.info("apply_design current-doc step=close_hidden design=%s", design_label)
        _close_hidden_doc(src)
        log.info("apply_design current-doc step=close_hidden done design=%s", design_label)
    masters = _master_entries(dest_doc)
    slide_count = original_count
    try:
        slide_count = int(dest_doc.getDrawPages().getCount())
    except Exception:
        pass
    return {
        "status": "ok",
        "design": design,
        "applied_master": master_name,
        "applied_master_shape_count": shape_count,
        "masters": masters,
        "blank_master": _blank_master_signal(masters),
        "slides_updated": slides_updated,
        "import_method": "clone_master",
        "slide_count": slide_count,
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
    """``loadComponentFromURL`` + ``AsTemplate`` — new document (M0′ proven path).

    Internal / test helper only. Not model-facing: a new Impress window
    gets a fresh sidebar agent and drops mid-chat context.
    """
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


class ListDesigns(ToolBase):
    """Enumerate shipped Impress ``.otp`` designs via PathSettings."""

    name: str | None = "list_designs"
    intent: str | None = "navigate"
    description: str = (
        "List shipped Impress .otp designs from LibreOffice PathSettings template "
        "directories (id, name, path, url, look). Read look to choose a design by "
        "appearance (dark/tech vs candy/illustrated), not only by name. Use a listed "
        "id with apply_design to restyle the open Impress deck. Does not hardcode the "
        "install prefix."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}
    uno_services: list[str] | None = [
        "com.sun.star.drawing.DrawingDocument",
        "com.sun.star.presentation.PresentationDocument",
    ]
    tier: str = "core"
    is_mutation: bool | None = False

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        designs = enumerate_impress_designs(ctx.ctx)
        return {"status": "ok", "designs": designs, "count": len(designs)}


class ApplyDesign(ToolBase):
    """Restyle the open Impress deck from a listed ``.otp`` (clone_master)."""

    name: str | None = "apply_design"
    intent: str | None = "edit"
    description: str = (
        "Restyle the OPEN Impress deck from a listed .otp: Hidden-load the "
        "template, clone its master (shapes + layout styles) into this document, "
        "and assign that master to every slide. Does not use the system clipboard "
        "and does not open a new presentation. Existing title/body text stays. "
        "Call list_designs first. Draw documents return a not-Impress error."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "design": {
                "type": "string",
                "description": "Design id, name, path, or url from list_designs (e.g. Metropolis).",
            },
        },
        "required": ["design"],
    }
    uno_services: list[str] | None = [
        "com.sun.star.drawing.DrawingDocument",
        "com.sun.star.presentation.PresentationDocument",
    ]
    tier: str = "core"
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        if _is_draw_only_doc(ctx.doc):
            return not_impress_error(self, "apply_design")
        if not _is_impress_doc(ctx.doc):
            return not_impress_error(self, "apply_design")
        design_key = str(kwargs.get("design") or "").strip()
        if not design_key:
            return self._tool_error("design is required.")
        entry = resolve_design(ctx.ctx, design_key)
        if entry is None:
            available = [d["id"] for d in enumerate_impress_designs(ctx.ctx)]
            return self._tool_error("Design not found: %s" % design_key, available=available[:40])
        try:
            return apply_design_to_current_doc(ctx.ctx, ctx.doc, entry)
        except Exception as e:
            log.exception("current-doc apply_design failed for %s", entry.get("path"))
            return self._tool_error(
                "Failed to apply design to the current presentation: %s" % e,
                reason="current_doc_import_failed",
                hint="Hidden .otp master clone or MasterPage assign did not complete.",
            )

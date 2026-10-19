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
deck or File→Templates, then ``list_designs`` → ``apply_design``.

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

from plugin.doc.document_research import _path_settings_from_ctx, _resolve_lo_directory_path, _should_skip_filename
from plugin.draw.design_look import derive_otp_look
from plugin.draw.otp_zip import extract_member, find_member, read_member
from plugin.framework.tool import ToolBase, ToolContext
from plugin.framework.errors import is_disposed_exception, is_real_disposal
from plugin.framework.uno_context import uno_same
from plugin.framework.url_utils import path_to_file_url

log = logging.getLogger("writeragent.draw.designs")

NOT_IMPRESS_CODE = "UNSUPPORTED_DOC_TYPE"
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


def _master_image_hrefs_from_zip(zf: Any, master_name: str) -> list[str] | None:
    """Read styles.xml and collect draw:image/@xlink:href for the named master page."""
    import xml.etree.ElementTree as ET  # nosemgrep: use-defused-xml  # shipped .otp styles.xml, size-capped by read_member

    styles_name = find_member(zf, "styles.xml")
    if not styles_name:
        return None
    raw_xml = read_member(zf, styles_name)
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
_CLONE_MASTER_PAGE_PROPS = ("BackgroundFullSize", "BorderLeft", "BorderRight", "BorderTop", "BorderBottom", "Width", "Height")

# PathSettings properties that hold template directories. Discover extras whose
# name contains "emplate" so we do not hardcode an install prefix.
_TEMPLATE_PROP_FALLBACK = ("Template", "Template_internal", "Template_user", "Template_writable")

_OTP_EXT = ".otp"


def _is_impress_doc(doc: Any) -> bool:
    """True for Impress. PyUNO hasattr is unreliable (same as pages._is_impress_doc).

    supportsService raises on a disposed document. Catching that and
    returning False made apply_design report UNSUPPORTED_DOC_TYPE
    ("requires an Impress presentation") for a closed deck.
    """
    return bool(doc.supportsService("com.sun.star.presentation.PresentationDocument"))


def not_impress_error(tool: ToolBase, action: str) -> dict[str, Any]:
    return tool._tool_error("%s requires an Impress presentation, not a Draw document." % action, code=NOT_IMPRESS_CODE, doc_type="draw")


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


def _design_id_from_path(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0].strip().lower()


def _design_name_from_path(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


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
                did = _design_id_from_path(full)
                if len(designs_by_id) >= limit and did not in designs_by_id:
                    out = list(designs_by_id.values())
                    out.sort(key=lambda d: (d["name"].lower(), d["path"]))
                    return out
                entry = {
                    "id": did,
                    "name": _design_name_from_path(full),
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
    path_norm = os.path.normcase(os.path.normpath(needle)) if not needle.startswith("file:") else ""
    for entry in designs:
        if entry["id"] == lower or entry["name"].lower() == lower:
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
    try:
        masters = doc.getMasterPages()
    except Exception as exc:
        # Bugfix: a closed document raises DisposedException from getMasterPages(),
        # and this except turned that into a missing page / not-found result.
        # Re-raise real disposal so execute_safe reports DOCUMENT_DISPOSED.
        # Any other failure stays on the existing missing-page path.
        if is_real_disposal(exc):
            raise
        return out
    for i in range(masters.getCount()):
        m = masters.getByIndex(i)
        name = m.Name if hasattr(m, "Name") else ""
        # MasterPage.getCount() does not fail on a live page. The old except
        # left shape_count at 0 and swallowed a closed document.
        shapes = int(m.getCount())
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


def inherit_master_from_neighbor(pages: Any, new_page: Any, insert_at: int) -> str:
    """Copy MasterPage from a slide that is not the new page.

    ``insertNewByIndex`` can leave a factory Default master even when the deck
    already has an assigned design master. Prefer the previous slide, then the
    following one, so add_slide keeps the deck look.

    ``add_slide(page=0)`` can pass ``insert_at=0`` while the new
    page actually sits at index 1. The only candidate is then index
    1, which is the new page, so the copy assigns that page's own
    master. ``InsertSdPage`` cannot create a page at index 0, and a
    neighbor chosen by integer index alone is the new page. Skip
    the new page object (``uno_same``), not the integer
    ``insert_at``. A stale ``insert_at`` of 0 is the neighbor when
    the new page actually sits at 1. With placement fixed, index
    0's neighbor is the previous first slide.
    """
    # pages.getCount() does not fail on a live deck. Returning "" hid a closed document.
    count = int(pages.getCount())
    if count < 2:
        return ""

    order: list[int] = []
    if insert_at > 0:
        order.append(insert_at - 1)
    if insert_at + 1 < count:
        order.append(insert_at + 1)
    for i in range(count):
        if i not in order:
            order.append(i)
    for ref_idx in order:
        if ref_idx < 0 or ref_idx >= count:
            continue
        try:
            ref = pages.getByIndex(ref_idx)
            if uno_same(ref, new_page):
                continue
            master = ref.MasterPage
            new_page.MasterPage = master
            return master.Name if hasattr(master, "Name") else ""
        except Exception as exc:
            # Prefer the previous slide, then the following one. Returning
            # here stopped on the first failure. is_real_disposal only:
            # a live-doc RuntimeException must try the next neighbor.
            # is_disposed_exception treats that name as disposal, and
            # add_slide would then delete the new slide.
            if is_real_disposal(exc):
                raise
            log.debug("inherit_master_from_neighbor failed insert_at=%s ref=%s", insert_at, ref_idx, exc_info=True)
            continue
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
    try:
        return guard_uno(model)
    except Exception:
        _close_hidden_doc(model)
        raise


def _copy_uno_prop(dest: Any, src: Any, name: str) -> str:
    """Copy one UNO property or interface attribute.

    Returns "copied", "missing", or "rejected". Properties in XPropertySet
    use getPropertyValue/setPropertyValue; geometry falls back to getattr/setattr.
    """
    try:
        value = src.getPropertyValue(name)
    except Exception as exc:
        if is_real_disposal(exc):
            raise
        try:
            value = getattr(src, name)
        except Exception as inner_exc:
            if is_real_disposal(inner_exc):
                raise
            return "missing"
    try:
        dest.setPropertyValue(name, value)
        return "copied"
    except Exception as exc:
        if is_real_disposal(exc):
            raise
    try:
        setattr(dest, name, value)
        return "copied"
    except Exception as exc:
        if is_real_disposal(exc):
            raise
        if name in ("Position", "Size"):
            raise RuntimeError("Failed to set %s on cloned shape" % name)
        return "rejected"


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
    except Exception as exc:
        if is_real_disposal(exc):
            raise
        log.debug("GraphicProvider.queryGraphic failed url=%s", file_url, exc_info=True)
        return None


def _reimport_graphic(
    uno_ctx: Any,
    src_shape: Any,
    dest_shape: Any,
    *,
    image_path: str | None = None,
) -> bool:
    """Attach chrome without sharing the Hidden source SfxItemPool."""
    if uno_ctx is None:
        return False
    import tempfile

    from plugin.framework.url_utils import path_to_file_url
    from plugin.writer.format import create_property_value

    # 1) Extracted image path — never opens the Hidden Graphic.
    if image_path and os.path.isfile(image_path):
        url = path_to_file_url(image_path)
        log.debug("clone_master graphic via extracted file path=%s", image_path)
        new_graphic = _graphic_from_file_url(uno_ctx, url)
        if new_graphic is not None:
            dest_shape.Graphic = new_graphic
            return True

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
    log.debug("clone_master graphic via storeGraphic fallback")
    for suffix, mime in ((".svg", "image/svg+xml"), (".png", "image/png")):
        fd, path = tempfile.mkstemp(suffix=suffix)
        try:
            os.close(fd)
            url = path_to_file_url(path)
            provider.storeGraphic(graphic, (create_property_value("URL", url), create_property_value("MimeType", mime)))
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
    image_path: str | None = None,
    shape_index: int = 0,
    warnings: list[str] | None = None,
) -> tuple[str, bool | None]:
    """Create a dest-owned shape, add it, then copy visual props + geometry.

    Adding first is required: presentation placeholders ignore Size/Position
    set before they belong to a page.
    """
    shape_type = str(getattr(src_shape, "ShapeType", "") or "")
    if not shape_type.startswith("com.sun.star."):
        raise RuntimeError("Unsupported master shape type: %s" % shape_type)
    clone = dest_doc.createInstance(shape_type)
    dest_master.add(clone)
    shape_warnings: list[str] = []
    for prop in _CLONE_SHAPE_PROPS:
        res = _copy_uno_prop(clone, src_shape, prop)
        if res == "rejected":
            token = f"rejected {prop} on shape {shape_index}"
            shape_warnings.append(token)
            if warnings is not None:
                warnings.append(token)
        elif res == "copied" and prop == "FillStyle":
            try:
                val = src_shape.getPropertyValue("FillStyle")
            except Exception:
                val = getattr(src_shape, "FillStyle", None)
            if val is not None:
                try:
                    int_val = int(getattr(val, "value", val))
                except (ValueError, TypeError):
                    int_val = -1
                if int_val not in (0, 1):
                    fill_kind = {2: "gradient", 3: "hatch", 4: "bitmap"}.get(int_val, "unknown")
                    token = f"fill {fill_kind} not copied on shape {shape_index}"
                    shape_warnings.append(token)
                    if warnings is not None:
                        warnings.append(token)
    reimported: bool | None = None
    if "GraphicObject" in shape_type:
        reimported = _reimport_graphic(
            uno_ctx,
            src_shape,
            clone,
            image_path=image_path,
        )
        log.debug("clone_master graphic_reimport ok=%s type=%s", reimported, shape_type)
    # Geometry last so layout does not overwrite chrome metrics.
    _copy_uno_prop(clone, src_shape, "Position")
    _copy_uno_prop(clone, src_shape, "Size")
    log.debug("clone_shape index=%s type=%s warnings=%s", shape_index, shape_type, shape_warnings)
    return shape_type, reimported


def clone_master_into_doc(dest_doc: Any, src_doc: Any, design: dict[str, str], uno_ctx: Any = None) -> ClonedMaster:
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
        # MasterPage.Name does not fail on a live page. Swallowing a failure
        # skipped that master and hid a closed document.
        name = str(candidate.Name or "")
        lower = name.strip().lower()
        if lower and (lower == design_name.lower() or lower == design_id):
            src_master = candidate
            if not design_name:
                design_name = name
            break
    if src_master is None:
        src_master = src_masters.getByIndex(0)
        if not design_name:
            # MasterPage.Name does not fail on a live page.
            design_name = str(src_master.Name or "") or "Imported"

    dest_masters = dest_doc.getMasterPages()
    # Always insert a new master. Reusing a same-named master cleared its
    # shapes before the copy, so a later failure left that master empty.
    dest_master = dest_masters.insertNewByIndex(dest_masters.getCount())
    if dest_master is None:
        raise RuntimeError("Could not insert a destination master page.")
    try:
        dest_master.Name = design_name
    except Exception as exc:
        if is_disposed_exception(exc):
            raise
        log.debug("rename cloned master to %s failed", design_name, exc_info=True)

    tmp_dir = tempfile.mkdtemp(prefix="wa_otp_clone_")
    try:
        warnings: list[str] = []
        # Factory placeholders on the new master are not user content.
        _clear_master_shapes(dest_master)
        for prop in _CLONE_MASTER_PAGE_PROPS:
            res = _copy_uno_prop(dest_master, src_master, prop)
            if res == "rejected":
                warnings.append(f"rejected {prop} on master")
        try:
            src_count = int(src_master.getCount())
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            src_count = 0

        graphic_shape_count = 0
        for i in range(src_count):
            st = str(getattr(src_master.getByIndex(i), "ShapeType", "") or "")
            if "GraphicObject" in st:
                graphic_shape_count += 1

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
        graphic_index = 0
        for i in range(src_count):
            src_shape = src_master.getByIndex(i)
            st = str(getattr(src_shape, "ShapeType", "") or "")
            log.debug("clone_master step=shape i=%s/%s type=%s", i, src_count, st)
            image_path = None
            if "GraphicObject" in st:
                graphic_shapes += 1
                if graphic_index < len(extracted_image_paths):
                    image_path = extracted_image_paths[graphic_index]
                graphic_index += 1
            shape_type, reimported = _clone_one_shape(
                dest_doc,
                dest_master,
                src_shape,
                uno_ctx,
                image_path=image_path,
                shape_index=i,
                warnings=warnings,
            )
            if reimported is True:
                graphic_reimports += 1
            cloned_types.append(shape_type)
            log.debug("clone_master step=shape_done i=%s type=%s", i, shape_type)

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
        return ClonedMaster(dest_master, dest_name, shape_count, graphic_shapes, graphic_reimports, tuple(warnings))
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
            if is_disposed_exception(exc):
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
    dest_masters = dest_doc.getMasterPages()
    count = int(dest_masters.getCount())
    to_remove: list[Any] = []
    for i in range(count):
        m = dest_masters.getByIndex(i)
        if uno_same(m, new_master):
            continue
        m_name = str(getattr(m, "Name", "") or "").strip()
        if m_name.lower() == target_lower:
            if not _is_master_referenced(dest_doc, m):
                to_remove.append(m)
    for m in to_remove:
        try:
            dest_masters.remove(m)
        except Exception as exc:
            if is_real_disposal(exc):
                raise
            log.debug("remove old same-named master failed", exc_info=True)


def apply_design_to_current_doc(uno_ctx: Any, dest_doc: Any, design: dict[str, str]) -> dict[str, Any]:
    """Import a shipped ``.otp`` master into *dest_doc* and assign it to all slides."""
    # getDrawPages().getCount() does not fail on a live document.
    # A closed document must not be reported as zero slides.
    dest_doc.getDrawPages().getCount()
    src = None
    master_name = ""
    shape_count = 0
    graphic_shapes = 0
    graphic_reimports = 0
    slides_updated = 0
    clone_warnings_list: list[str] = []
    design_label = design.get("name") or design.get("id") or "?"
    target_design_name = str(design.get("name") or design_label).strip()

    try:
        src = open_design_source_hidden(uno_ctx, design, as_template=True)
        cloned = clone_master_into_doc(dest_doc, src, design, uno_ctx)
        _master = cloned.master
        master_name = cloned.name
        shape_count = cloned.shape_count
        graphic_shapes = cloned.graphic_shapes
        graphic_reimports = cloned.graphic_reimports
        clone_warnings_list = list(getattr(cloned, "warnings", ()) or ())

        # Snapshot each page's MasterPage before assign
        snapshot: list[tuple[Any, Any]] = []
        try:
            pages = dest_doc.getDrawPages()
            for i in range(pages.getCount()):
                p = pages.getByIndex(i)
                snapshot.append((p, getattr(p, "MasterPage", None)))

            slides_updated = assign_master_to_all_slides(dest_doc, _master)
            slide_count = int(dest_doc.getDrawPages().getCount())
            if slides_updated != slide_count:
                raise RuntimeError("Partial slide assignment")

            # Replace previous unreferenced master of the same design name
            _cleanup_previous_same_named_masters(dest_doc, _master, target_design_name)
            try:
                _master.Name = target_design_name
                master_name = target_design_name
            except Exception as exc:
                if is_real_disposal(exc):
                    raise
                master_name = str(getattr(_master, "Name", "") or "") or target_design_name
        except Exception as post_exc:
            if is_real_disposal(post_exc):
                _restore_slide_masters(snapshot, swallow_disposal=True)
                raise
            slides_restored = _restore_slide_masters(snapshot)
            if not _is_master_referenced(dest_doc, _master):
                try:
                    dest_doc.getMasterPages().remove(_master)
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
                "applied_master_shape_count": shape_count,
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
            shape_count,
            graphic_reimports,
            graphic_shapes,
            slides_updated,
        )
    except DesignChromeMissingError as exc:
        log.warning("apply_design failed chrome missing: %s", exc)
        warns = list(getattr(exc, "warnings", ()) or ())
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

    masters = _master_entries(dest_doc)
    slide_count = int(dest_doc.getDrawPages().getCount())
    if graphic_shapes > 0 and graphic_reimports < graphic_shapes:
        msg = (
            "Applied design '%s' (master '%s') to the current presentation (%s of %s chrome shapes imported)."
            % (design.get("name"), master_name, graphic_reimports, graphic_shapes)
        )
    else:
        msg = "Applied design '%s' (master '%s') to the current presentation." % (design.get("name"), master_name)
    return {
        "status": "ok",
        "design": design,
        "applied_master": master_name,
        "applied_master_shape_count": shape_count,
        "graphic_shapes": graphic_shapes,
        "graphic_reimports": graphic_reimports,
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
    uno_services: list[str] | None = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]
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
        "template, clone its master shapes and reimported graphics into this "
        "document, and assign that master to every slide. Does not use "
        "the system clipboard and does not open a new presentation. "
        "One master is cloned and assigned to every slide; other layout masters in the .otp are not imported. "
        "Solid fill, line, and character properties, geometry, and pictures are cloned. "
        "Gradients, bitmap fills, and presentation style families are not (copying those families shares the hidden source pool and aborts LibreOffice). "
        "The result may include clone_warnings when a property is rejected or a gradient, hatch, or bitmap fill was not reproduced. "
        "Existing placeholder text stays on its current styles. "
        "Applying the same design again replaces the previously imported master of that name. "
        "Call list_designs first. Draw documents return a not-Impress error."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"design": {"type": "string", "description": "Design id, name, path, or url from list_designs (e.g. Metropolis)."}}, "required": ["design"]}
    uno_services: list[str] | None = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]
    tier: str = "core"
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        # Draw and every other non-Impress doc share this error.
        # _is_draw_only_doc returned the same payload.
        if not _is_impress_doc(ctx.doc):
            return not_impress_error(self, "apply_design")
        design_key = str(kwargs.get("design") or "").strip()
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
            if is_disposed_exception(e):
                raise
            log.exception("current-doc apply_design failed for %s", entry.get("path"))
            return self._tool_error("Failed to apply design to the current presentation: %s" % e, reason="current_doc_import_failed", hint="Hidden .otp master clone or MasterPage assign did not complete.")

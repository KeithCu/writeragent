# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Writer page tools (page domain, specialized tier).

Page styles, margins, headers/footers, columns, and page breaks.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import TYPE_CHECKING, Any, cast

from plugin.framework.errors import is_disposed_exception, is_real_disposal, make_tool_error, resolve_exception_message
from plugin.framework.uno_context import uno_same
from .edit_review import flag_style_unreviewed
from .format import PAGE_NUMBER_NUMBERING_TYPE_NAMES, record_walk_cap
from .specialized_base import ToolWriterPageBase

log = logging.getLogger("writeragent.writer")


def _mm_to_hmm(val: Any) -> int:
    """Convert mm to 1/100 mm integer. Rejects bool, non-numbers, and negative values."""
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"Expected a non-negative number of mm, got {val!r}")
    if val < 0:
        raise ValueError(f"mm value must be non-negative, got {val!r}")
    # round before int avoids 2.3 * 100 truncating to 229 instead of 230
    return int(round(float(val) * 100))


def _uno_len(text: str) -> int:
    """Length in UTF-16 code units (what LibreOffice XText cursor positions count)."""
    return len(text.encode("utf-16-le")) // 2


_LAYOUT_NAMES = ("ALL", "LEFT", "RIGHT", "MIRRORED")


def _page_style_layout_to_int(raw: Any) -> int | None:
    """Map PageStyleLayout enum or int to 0..3 (or passthrough int). None if unknown."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    name = getattr(raw, "value", None)
    if isinstance(name, str):
        upper = name.strip().upper()
        if upper in _LAYOUT_NAMES:
            return _LAYOUT_NAMES.index(upper)
    return None


def _page_style_layout_to_uno(val: Any) -> Any:
    """Validate int 0..3 and return UNO PageStyleLayout enum constant."""
    if val not in (0, 1, 2, 3):
        raise ValueError("page_style_layout must be 0=ALL, 1=LEFT, 2=RIGHT, or 3=MIRRORED.")
    from com.sun.star.style.PageStyleLayout import ALL, LEFT, RIGHT, MIRRORED
    return (ALL, LEFT, RIGHT, MIRRORED)[val]


def _mirrored_region_skip(style_name: str, region: str) -> dict[str, Any]:
    shared = _region_kind(region)
    return {
        "status": "skipped",
        "style": style_name,
        "region": region,
        "mirrored": True,
        "message": (
            f"{region} mirrors the shared {shared}. Edit region='{shared}', "
            f"or turn sharing off (first_is_shared / header_is_shared / footer_is_shared) "
            f"before writing a distinct first or left region."
        ),
    }


def _in_text_table(cursor: Any) -> bool:
    """True when the cursor is positioned inside a table cell."""
    try:
        tbl = cursor.getPropertyValue("TextTable")
        return tbl is not None
    except Exception as e:
        if is_disposed_exception(e):
            raise
        return False


_PAGE_SET_SPECS = (
    ("width_mm", "width", "Width", "mm"),
    ("height_mm", "height", "Height", "mm"),
    ("left_margin_mm", "left_margin", "LeftMargin", "mm"),
    ("right_margin_mm", "right_margin", "RightMargin", "mm"),
    ("top_margin_mm", "top_margin", "TopMargin", "mm"),
    ("bottom_margin_mm", "bottom_margin", "BottomMargin", "mm"),
    ("gutter_margin_mm", "gutter_margin", "GutterMargin", "mm"),
    ("header_is_on", "header_is_on", "HeaderIsOn", "bool"),
    ("footer_is_on", "footer_is_on", "FooterIsOn", "bool"),
    ("header_is_shared", "header_is_shared", "HeaderIsShared", "bool"),
    ("footer_is_shared", "footer_is_shared", "FooterIsShared", "bool"),
    ("first_is_shared", "first_is_shared", "FirstIsShared", "bool"),
    ("header_height_mm", "header_height", "HeaderHeight", "mm"),
    ("footer_height_mm", "footer_height", "FooterHeight", "mm"),
    ("header_body_distance_mm", "header_body_distance", "HeaderBodyDistance", "mm"),
    ("footer_body_distance_mm", "footer_body_distance", "FooterBodyDistance", "mm"),
    ("back_color", "back_color", "BackColor", "color_int"),
    ("back_transparent", "back_transparent", "BackTransparent", "bool"),
    ("numbering_type", "numbering_type", "NumberingType", "int"),
    ("footnote_height_mm", "footnote_height", "FootnoteHeight", "mm"),
    ("register_paragraph_style", "register_paragraph_style", "RegisterParagraphStyle", "str"),
    ("page_style_layout", "page_style_layout", "PageStyleLayout", "layout"),
)

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

# region name -> (is_on property, text property)
#
# The *_first and *_left variants are separate UNO text objects, not views of the same one: a
# "different first page" letterhead (FirstIsShared=False) lives ONLY in HeaderTextFirst, and with
# HeaderIsShared=False the plain HeaderText covers right-hand pages alone. Exposing just the
# shared pair left both cases unreachable — read and write silently hit the wrong page.
# When the corresponding *IsShared* flag is on, the variant mirrors the shared text, so asking for
# it is always safe.
_REGION_PROPS = {
    "header": ("HeaderIsOn", "HeaderText"),
    "footer": ("FooterIsOn", "FooterText"),
    "header_first": ("HeaderIsOn", "HeaderTextFirst"),
    "footer_first": ("FooterIsOn", "FooterTextFirst"),
    "header_left": ("HeaderIsOn", "HeaderTextLeft"),
    "footer_left": ("FooterIsOn", "FooterTextLeft"),
}

_REGIONS = tuple(_REGION_PROPS)

# In-flow header/footer HTML does not carry draw-page shapes. Probe C4: a new
# TextGraphicObject with these props keeps a floating hyperlink in the clone.
_PAGE_GRAPHIC_PROPS = (
    "AnchorType", "Graphic", "Width", "Height",
    "HoriOrient", "HoriOrientPosition", "HoriOrientRelation",
    "VertOrient", "VertOrientPosition", "VertOrientRelation",
    "HyperLinkURL", "HyperLinkTarget", "Title", "Description",
)

# Walk caps for _scan_region_content. A header/footer is small; these only bound a runaway.
_SCAN_PARA_LIMIT = 5000
_SCAN_PORTION_LIMIT = 5000
_SCAN_SHAPE_LIMIT = 5000


def _region_kind(region: str) -> str:
    """'header' or 'footer' for any region name (the shared one or a first/left variant)."""
    return "header" if region.startswith("header") else "footer"


def _empty_scan() -> dict[str, Any]:
    """The shape _scan_region_content returns, for a region with nothing to scan."""
    return {"fields": [], "images": [], "paragraph_count": 0}


def _region_has_table(text_obj: Any, *, strict: bool = False) -> bool:
    """True when the region's enumeration includes a text table (letterhead grid).

    ``strict`` re-raises read failures instead of reading them as "no table".
    """
    try:
        enum = text_obj.createEnumeration()
    except Exception:
        if strict:
            raise
        return False
    seen = 0
    while enum.hasMoreElements() is True and seen < _SCAN_PARA_LIMIT:
        seen += 1
        try:
            el = enum.nextElement()
        except Exception:
            if strict:
                raise
            break
        try:
            # `is True`: MagicMock supportsService() is truthy and would false-positive.
            if el.supportsService("com.sun.star.text.TextTable") is True:
                return True
        except Exception:
            if strict:
                raise
            continue
    return False


def _region_holds_content(doc: Any, text_obj: Any | None) -> bool:
    """True if the header/footer XText still holds text, fields, images, or tables.

    F5: disabling the region (``HeaderIsOn`` / ``FooterIsOn`` = false) *clears*
    its content. A ``None`` text object is the usual off-state absence. The
    #646 refuse uses this so ``*_is_on=false`` does not silently wipe — same
    idea as LibreOffice's delete-header prompt. Clear via ``page_set`` first,
    then turn off.
    ``getString()`` misses logos and can hide tables; the #638 region scan plus a
    table walk covers those. Every read is strict: a failed read raises rather
    than passing for "no content", so the guard fails closed.
    """
    if text_obj is None:
        return False
    plain = text_obj.getString()
    if isinstance(plain, str) and plain.strip():
        return True
    scan = _scan_region_content(doc, text_obj, strict=True)
    if scan["fields"] or scan["images"]:
        return True
    return _region_has_table(text_obj, strict=True)


def _region_mirrors_shared(style: Any, region: str) -> bool:
    """True when a first/left variant is only a view of the shared region.

    ``FirstIsShared=True`` means HeaderTextFirst / FooterTextFirst mirror
    HeaderText / FooterText; ``HeaderIsShared`` / ``FooterIsShared`` do the
    same for ``*_left``. Windows can still report leftover ``getString()``
    on the mirror after the shared region was cleared (GHA 35466498641),
    which made the disable guard refuse ``header_first`` even though first
    page is shared. ``is True`` so unit-test MagicMocks stay conservative
    (treated as independent).
    """
    if region.endswith("_first"):
        flag = "FirstIsShared"
    elif region.endswith("_left"):
        flag = "HeaderIsShared" if region.startswith("header") else "FooterIsShared"
    else:
        return False
    try:
        return style.getPropertyValue(flag) is True
    except Exception:
        return False


def _disable_blocked_by_content(doc: Any, style: Any, kwargs: dict[str, Any]) -> str | None:
    """Error text if ``header_is_on=false`` / ``footer_is_on=false`` would wipe content.

    F5: turning the region off *clears* its content (not "LO keeps HeaderText").
    Refuse ``*_is_on=false`` while content remains so we don't silently wipe —
    same idea as LibreOffice's delete-header prompt. Clear via ``page_set``
    first, then turn off.

    ``HeaderIsOn`` / ``FooterIsOn`` are the only toggles (no first/left IsOn props).
    Turning one off drops every variant of that kind, so independent matching
    regions are scanned. Shared-mirror first/left variants are skipped — they
    are the same XText when the matching ``*IsShared`` flag is on. Enabling
    (``true``) is never blocked. No force-off flag. A region that cannot be
    read refuses too: an unreadable region is not an empty one. Disposal raises.
    """
    for kw, kind in (("header_is_on", "header"), ("footer_is_on", "footer")):
        if kw not in kwargs or kwargs[kw]:
            continue
        held: list[str] = []
        for region, (_unused_is_on, text_prop) in _REGION_PROPS.items():
            if _region_kind(region) != kind:
                continue
            if _region_mirrors_shared(style, region):
                continue
            try:
                text_obj = style.getPropertyValue(text_prop)
                if _region_holds_content(doc, text_obj):
                    held.append(region)
            except Exception as e:
                if is_real_disposal(e):
                    raise
                log.warning("page: cannot read %s to check it is empty", region, exc_info=True)
                return (
                    "Cannot turn the %s off: reading %s failed (%s), so it may still have "
                    "content that turning it off would delete. Nothing was changed."
                    % (kind, region, resolve_exception_message(e))
                )
        if not held:
            continue
        clears = "; ".join(
            "page_set_header_footer_text(region='%s', content='')" % region for region in held
        )
        return (
            "Cannot turn the %s off while it still has content (%s). "
            "LibreOffice asks before deleting header/footer contents. "
            "Clear first with %s, then page_set_style_properties(%s=false)."
            % (kind, ", ".join(held), clears, kw)
        )
    return None


def _scan_region_content(doc: Any, text_obj: Any, *, strict: bool = False) -> dict[str, Any]:
    """Report fields and anchored images in a header/footer as get-side extras.

    HTML ``content`` already carries structure; these lists are machine-readable so a
    caller can see logos and live fields without parsing the markup. Returns
    ``{"fields": [...], "images": [...], "paragraph_count": int}``. ``strict``
    (the disable guard) re-raises read failures instead of skipping them.
    """
    out = _empty_scan()
    try:
        para_enum = text_obj.createEnumeration()
    except Exception:
        if strict:
            raise
        return out
    notes: list[str] = []
    # `is True` and the hard caps match the enumeration walk in format.py: a UNO enumeration that
    # misbehaves otherwise spins here forever.
    while para_enum.hasMoreElements() is True and out["paragraph_count"] < _SCAN_PARA_LIMIT:
        try:
            para = para_enum.nextElement()
        except Exception:
            if strict:
                raise
            break
        out["paragraph_count"] += 1
        try:
            portions = para.createEnumeration()
        except Exception:
            if strict:
                raise
            continue
        seen_portions = 0
        while portions.hasMoreElements() is True and seen_portions < _SCAN_PORTION_LIMIT:
            seen_portions += 1
            try:
                portion = portions.nextElement()
                if portion.getPropertyValue("TextPortionType") != "TextField":
                    continue
                field = portion.getPropertyValue("TextField")
            except Exception:
                if strict:
                    raise
                break
            try:
                # Same argument sense as fields_list: presentation is what the reader sees
                # ("1"), content is what the field IS ("Page number"). Swapping them would have
                # the two tools describe the same field in opposite terms.
                out["fields"].append({"presentation": field.getPresentation(False),
                                      "content": field.getPresentation(True)})
            except Exception:
                out["fields"].append({"presentation": "", "content": ""})
        record_walk_cap(portions, seen_portions, _SCAN_PORTION_LIMIT, "text portions", notes)
    record_walk_cap(para_enum, out["paragraph_count"], _SCAN_PARA_LIMIT, "paragraphs", notes)
    if notes:
        out["warning"] = notes[0]
    # Images anchored in the region live on the draw page, whatever their anchor type, so they are
    # found by matching the anchor's text object rather than by walking portions.
    try:
        draw_page = doc.getDrawPage()
        for i in range(min(int(draw_page.getCount()), _SCAN_SHAPE_LIMIT)):
            shape = draw_page.getByIndex(i)
            try:
                # Distinct PyUNO wrappers for the same header XText used to make
                # ``!=`` skip the logo (false miss). uno_same is the identity
                # ladder; a miss here is still wrong for get/metadata (and was
                # the disaster when wipe used this scan as a refuse gate).
                # A page-anchored shape has no anchor text range.
                anchor = shape.getAnchor()
                if anchor is None or not uno_same(anchor.getText(), text_obj):
                    continue
            except Exception:
                if strict:
                    raise
                continue
            try:
                out["images"].append(str(shape.getName()) or "unnamed")
            except Exception:
                out["images"].append("unnamed")
    except Exception:
        if strict:
            raise
    return out


def _height_props(region: str) -> tuple[str, str, str]:
    """Return the (dynamic-height, dynamic-spacing, height) property names."""
    prefix = "Header" if _region_kind(region) == "header" else "Footer"
    return (prefix + "IsDynamicHeight", prefix + "DynamicSpacing", prefix + "Height")


def set_header_footer_auto_height(style: Any, region: str, enabled: bool) -> None:
    """Let the region grow with its content (or pin it to a fixed height).

    Without this, a header keeps its fixed height and taller content —
    a letterhead logo, say — overlaps the text and spills into the body.
    """
    dyn_prop, spacing_prop, _unused = _height_props(region)
    style.setPropertyValue(dyn_prop, bool(enabled))
    try:
        style.setPropertyValue(spacing_prop, bool(enabled))
    except Exception:
        pass  # not offered by every page style


def resolve_page_style(doc: Any, style_name: str = "Standard") -> tuple[Any, str]:
    """Return ``(style_object, resolved_name)`` for a Writer page style."""
    styles = doc.getStyleFamilies().getByName("PageStyles")
    if not styles.hasByName(style_name):
        available = list(styles.getElementNames())
        raise ValueError("No page style named '%s'. Available: %s" % (style_name, ", ".join(available)))
    return styles.getByName(style_name), style_name


def _shape_name(shape: Any) -> str:
    try:
        name = shape.getName()
    except Exception:
        return "unnamed"
    text = str(name or "").strip()
    return text or "unnamed"


def _copy_region_graphics(doc: Any, src_text: Any, dest_text: Any) -> tuple[int, list[str]]:
    """Re-create floating graphics anchored in src_text inside dest_text.

    Shapes live on the draw page, matched with uno_same the same way as
    _scan_region_content. The walk snapshots the page first: insertTextContent
    adds the clone, and continuing over getCount() would copy that clone too.
    Non-graphic shapes are reported, not copied.
    """
    copied = 0
    skipped: list[str] = []
    try:
        draw_page = doc.getDrawPage()
        count = int(draw_page.getCount())
    except Exception:
        return 0, []
    shapes: list[Any] = []
    for i in range(min(count, _SCAN_SHAPE_LIMIT)):
        try:
            shapes.append(draw_page.getByIndex(i))
        except Exception:
            break
    for shape in shapes:
        try:
            if not uno_same(shape.getAnchor().getText(), src_text):
                continue
        except Exception:
            continue
        label = _shape_name(shape)
        try:
            # `is True`: MagicMock supportsService() is truthy and would copy a frame.
            if shape.supportsService("com.sun.star.text.TextGraphicObject") is not True:
                skipped.append(label)
                continue
        except Exception:
            skipped.append(label)
            continue
        try:
            clone = doc.createInstance("com.sun.star.text.TextGraphicObject")
            if not clone:
                skipped.append(label)
                continue
            for prop in _PAGE_GRAPHIC_PROPS:
                try:
                    clone.setPropertyValue(prop, shape.getPropertyValue(prop))
                except Exception:
                    log.debug("page style clone: graphic %s has no %s", label, prop)
            cursor = dest_text.createTextCursor()
            cursor.gotoEnd(False)
            dest_text.insertTextContent(cursor, clone, False)
            copied += 1
        except Exception:
            log.warning("page style clone: could not copy graphic %s", label, exc_info=True)
            skipped.append(label)
    return copied, skipped


def _region_has_flow_content(doc: Any, text_obj: Any) -> bool:
    """True when the region has text, fields, or a table the in-document copy must keep.

    A floating graphic alone is not flow content. _copy_region_graphics copies
    it after the portion walk.
    """
    try:
        plain = text_obj.getString()
    except Exception:
        plain = ""
    if isinstance(plain, str) and plain.strip():
        return True
    if _scan_region_content(doc, text_obj)["fields"]:
        return True
    return _region_has_table(text_obj)


def _copy_page_regions(
    doc: Any,
    source: Any,
    clone: Any,
    *,
    uno_ctx: Any,
    services: Any,
) -> tuple[int, list[str]]:
    """Copy on, non-mirror header/footer regions onto the clone.

    uno_ctx and services are unused. They stay so copy_page_style's keyword
    call does not change. Flow is copied in-document; this no longer opens a
    hidden Writer or reads the HTML import config service.
    """
    shapes_copied = 0
    skipped_shapes: list[str] = []
    for region, (is_on_prop, text_prop) in _REGION_PROPS.items():
        # First/left variants that only mirror the shared region would duplicate it.
        if _region_mirrors_shared(source, region):
            continue
        try:
            if source.getPropertyValue(is_on_prop) is not True:
                continue
        except Exception:
            continue
        try:
            src_text = source.getPropertyValue(text_prop)
        except Exception:
            src_text = None
        if src_text is None:
            continue
        clone.setPropertyValue(is_on_prop, True)

        # Issue #1424: Apply size values after turning the region on
        prefix = "Header" if region.startswith("header") else "Footer"
        for size_prop in (
            "IsDynamicHeight", "DynamicSpacing",
            "BodyDistance", "Height",
            "LeftMargin", "RightMargin"
        ):
            prop_name = f"{prefix}{size_prop}"
            try:
                clone.setPropertyValue(prop_name, source.getPropertyValue(prop_name))
            except Exception as e:
                log.debug("_copy_page_regions failed to copy %s: %s", prop_name, e)

        dest_text = clone.getPropertyValue(text_prop)
        if dest_text is None:
            raise RuntimeError("Page style clone: no %s text on the new style." % region)
        if _region_has_flow_content(doc, src_text):
            # Bugfix: this used to export the region through xtext_to_content
            # (controller.select + clipboard + hidden Writer + in-process
            # libxslt / flat ODF) and import it with replace_xtext_with_html.
            # That export is what corrupted soffice (malloc unsorted-list /
            # SIGSEGV 139 / SalAbort), observed on the next getByName as
            # "Binary URP bridge disposed" (GHA 38526445257, 38584920511,
            # 38584636315). Same-document portion copy does not select, does
            # not use the clipboard, and does not run libxslt.
            from .html_export import _copy_flow_into_xtext

            fields_copied = _copy_flow_into_xtext(doc, src_text, dest_text)
            try:
                plain = dest_text.getString() or ""
            except Exception:
                plain = ""
            if not str(plain).strip() and fields_copied == 0 and not _region_has_table(dest_text):
                raise RuntimeError(
                    "Page style clone: copied %s flow content was empty." % region
                )
        copied, skipped = _copy_region_graphics(doc, src_text, dest_text)
        shapes_copied += copied
        skipped_shapes.extend(skipped)
    return shapes_copied, skipped_shapes


# Page-style UNO objects created by copy_page_style, held on purpose.
#
# LibreOffice race (crash seen as "Binary URP bridge disposed" on the next
# pages.getByName(new_name), soffice "Fatal exception: Signal 6", frame
# ImplInheritanceHelper<SwXBaseStyle,...>::queryInterface in libswlo):
# when Python drops its last reference to the created SwXPageStyle, the
# release goes out as a oneway URP call and runs on a separate threadpool
# thread. Its refcount reaches 0 and the destructor queues for the
# SolarMutex. A getByName for the same name that already holds the mutex
# finds that dying object through the style pool's listeners
# (SwXStyleFamily FindStyle), takes a new reference to it and returns it.
# The destructor then frees it, and the caller's first call on the result
# (the bridge's queryInterface) jumps through a freed vtable.
# If we keep the proxy alive, the final release never meets a lookup. The
# bound only caps memory. An evicted entry is many clones old.
_PAGE_STYLE_KEEPALIVE: deque[Any] = deque(maxlen=256)


def copy_page_style(
    doc: Any,
    source_name: str,
    new_name: str,
    *,
    uno_ctx: Any = None,
    services: Any = None,
) -> dict[str, Any]:
    """Clone a page style: direct properties, header/footer flow, floating graphics.

    FollowStyle is not copied. A new page style's FollowStyle defaults to itself
    (probe C3); copying the source's would chain the clone into the old next style.
    Called by style_clone. Not a tool of its own.

    The direct-property helper lives in styles.py. This function imports it at
    call time because styles.py already imports this module at load.
    """
    from .styles import _copy_direct_style_properties

    source, source_name = resolve_page_style(doc, source_name)
    styles = doc.getStyleFamilies().getByName("PageStyles")
    if styles.hasByName(new_name):
        raise ValueError("Style '%s' already exists in PageStyles." % new_name)
    clone = doc.createInstance("com.sun.star.style.PageStyle")
    if not clone:
        raise RuntimeError("Failed to create a page style instance.")
    try:
        parent = str(source.getParentStyle() or "")
    except Exception:
        parent = ""
    if parent:
        clone.setParentStyle(parent)
    # Insert before header/footer copy: those XText objects exist once the style
    # is in the family. Direct props are copied after insert so FollowStyle has
    # already defaulted to the new name and the skip list can leave it there.
    styles.insertByName(new_name, clone)
    _PAGE_STYLE_KEEPALIVE.append(clone)
    try:
        copied, skipped = _copy_direct_style_properties(source, clone)
        shapes_copied, skipped_shapes = _copy_page_regions(
            doc, source, clone, uno_ctx=uno_ctx, services=services,
        )
    except Exception:
        try:
            if styles.hasByName(new_name):
                styles.removeByName(new_name)
        except Exception:
            log.warning(
                "page style clone: could not remove half-created '%s'", new_name, exc_info=True,
            )
        raise
    return {
        "properties_copied": len(copied),
        "skipped_properties": skipped,
        "shapes_copied": shapes_copied,
        "skipped_shapes": skipped_shapes,
    }


def _mm(style: Any, name: str) -> float | None:
    """A 1/100 mm page-style property in mm, or None while LibreOffice leaves it void.

    HeaderHeight, HeaderBodyDistance, FooterHeight and
    FooterBodyDistance are void (Python None) while that header or
    footer is off, which is how every new Writer document starts
    (checked live). ``None / 100.0`` raises TypeError, and the whole
    read fails with "Error reading properties from page style
    'Standard'". A void measure is reported as None (not applicable),
    and the rest of the style still reads.
    """
    value = style.getPropertyValue(name)
    return None if value is None else value / 100.0


def get_page_style_properties(doc: Any, style_name: str = "Standard") -> dict[str, Any]:
    """Read dimensions, margins, and header/footer state of a Writer page style.

    Shared by ``page_get_style_properties`` and ``style_get_info(family=PageStyles)``.
    A future Option B could hard-merge those two public APIs into one (winner TBD:
    general ``style_get_info`` vs the page toolkit); deferred for now.
    """
    try:
        style, style_name = resolve_page_style(doc, style_name)
    except ValueError as e:
        return make_tool_error(str(e))

    props = {
        "style_name": style_name,
        "width_mm": _mm(style, "Width"),
        "height_mm": _mm(style, "Height"),
        "is_landscape": style.getPropertyValue("IsLandscape"),
        "left_margin_mm": _mm(style, "LeftMargin"),
        "right_margin_mm": _mm(style, "RightMargin"),
        "top_margin_mm": _mm(style, "TopMargin"),
        "bottom_margin_mm": _mm(style, "BottomMargin"),
        "gutter_margin_mm": _mm(style, "GutterMargin"),
        "header_is_on": style.getPropertyValue("HeaderIsOn"),
        "footer_is_on": style.getPropertyValue("FooterIsOn"),
        "header_is_shared": style.getPropertyValue("HeaderIsShared"),
        "footer_is_shared": style.getPropertyValue("FooterIsShared"),
        "header_height_mm": _mm(style, "HeaderHeight"),
        "footer_height_mm": _mm(style, "FooterHeight"),
        "header_body_distance_mm": _mm(style, "HeaderBodyDistance"),
        "footer_body_distance_mm": _mm(style, "FooterBodyDistance"),
        "back_color": style.getPropertyValue("BackColor"),
        "back_transparent": style.getPropertyValue("BackTransparent"),
        "numbering_type": style.getPropertyValue("NumberingType"),
        "footnote_height_mm": _mm(style, "FootnoteHeight"),
    }
    # RegisterParagraphStyle is optional and missing in some page styles; isolate it
    # so one missing property doesn't fail the read.
    try:
        props["register_paragraph_style"] = style.getPropertyValue("RegisterParagraphStyle")
    except Exception:
        pass
    # False means the first page has its OWN header/footer — the usual setup for a
    # letterhead — reachable only through the header_first / footer_first regions. Fetched
    # separately: not every page style offers it, and one missing property must not sink
    # the whole read.
    try:
        props["first_is_shared"] = style.getPropertyValue("FirstIsShared")
    except Exception:
        pass
    # PageStyleLayout enum or int -> int 0=ALL, 1=LEFT, 2=RIGHT, 3=MIRRORED
    try:
        psl = _page_style_layout_to_int(style.getPropertyValue("PageStyleLayout"))
        if psl is not None:
            props["page_style_layout"] = psl
    except Exception:
        pass
    return {"status": "ok", "properties": props}


# ------------------------------------------------------------------
# PageGetStyleProperties
# ------------------------------------------------------------------


class PageGetStyleProperties(ToolWriterPageBase):
    """Get dimensions, margins, and header/footer states of a page style."""

    name: str | None = "page_get_style_properties"
    description: str = "Get dimensions, margins, and header/footer states of a page style."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"style": {"type": "string", "description": "The name of the page style (e.g., 'Standard' or 'Default Style'). Defaults to 'Standard'."}}, "required": []}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        return get_page_style_properties(ctx.doc, kwargs.get("style", "Standard"))


# ------------------------------------------------------------------
# PageSetStyleProperties
# ------------------------------------------------------------------


class PageSetStyleProperties(ToolWriterPageBase):
    """Modify dimensions, margins, and header/footer toggles of a page style."""

    name: str | None = "page_set_style_properties"
    description: str = (
        "Modify dimensions, margins, and header/footer toggles of a page style. "
        "header_is_on=false / footer_is_on=false refuse if that region still has "
        "text, fields, images, or tables — clear with page_set_header_footer_text "
        "first, then disable. Enabling (true) is always allowed. No force-off."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "style": {"type": "string", "description": "The name of the page style (e.g., 'Standard' or 'Default Style'). Defaults to 'Standard'."},
            "width_mm": {"type": "number", "description": "New width in mm."},
            "height_mm": {"type": "number", "description": "New height in mm."},
            "is_landscape": {"type": "boolean", "description": "Sets the IsLandscape flag only. Does not change Width or Height unless width_mm and height_mm are also passed. Landscape A4 is width_mm 297 and height_mm 210."},
            "left_margin_mm": {"type": "number", "description": "Left margin in mm."},
            "right_margin_mm": {"type": "number", "description": "Right margin in mm."},
            "top_margin_mm": {"type": "number", "description": "Top margin in mm."},
            "bottom_margin_mm": {"type": "number", "description": "Bottom margin in mm."},
            "gutter_margin_mm": {"type": "number", "description": "Gutter margin in mm (for binding)."},
            "header_is_on": {
                "type": "boolean",
                "description": (
                    "Enable the header, or disable it when empty. false refuses while "
                    "any header region still holds text, fields, images, or tables — "
                    "clear with page_set_header_footer_text first (LibreOffice asks "
                    "before deleting header contents)."
                ),
            },
            "footer_is_on": {
                "type": "boolean",
                "description": (
                    "Enable the footer, or disable it when empty. false refuses while "
                    "any footer region still holds text, fields, images, or tables — "
                    "clear with page_set_header_footer_text first."
                ),
            },
            "header_is_shared": {"type": "boolean", "description": "Share header between left/right pages."},
            "footer_is_shared": {"type": "boolean", "description": "Share footer between left/right pages."},
            "first_is_shared": {"type": "boolean", "description": (
                "Share the header/footer with the first page. Set false for a 'different first page' "
                "letterhead; its content then lives in the header_first / footer_first regions.")},
            "header_height_mm": {"type": "number", "description": "Absolute header height in mm."},
            "footer_height_mm": {"type": "number", "description": "Absolute footer height in mm."},
            "header_body_distance_mm": {"type": "number", "description": "Spacing from header to body in mm."},
            "footer_body_distance_mm": {"type": "number", "description": "Spacing from footer to body in mm."},
            "back_color": {"type": "integer", "description": "Background color (RGB long)."},
            "back_transparent": {"type": "boolean", "description": "Make background transparent."},
            "numbering_type": {
                "type": "integer",
                "description": f"com.sun.star.style.NumberingType short ({', '.join(f'{k}={PAGE_NUMBER_NUMBERING_TYPE_NAMES[k]}' for k in (0, 1, 2, 3, 4))}).",
            },
            "footnote_height_mm": {"type": "number", "description": "Max footnote area height in mm."},
            "register_paragraph_style": {"type": "string", "description": "Register true reference style."},
            "page_style_layout": {"type": "integer", "description": "0=ALL, 1=LEFT, 2=RIGHT, 3=MIRRORED"},
        },
        "required": [],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        style_name = kwargs.get("style", "Standard")
        doc = ctx.doc

        try:
            style, style_name = resolve_page_style(doc, style_name)
        except ValueError as e:
            return self._tool_error(str(e))

        # F5: HeaderIsOn/FooterIsOn=false *clears* content. Refuse while
        # content remains so we don't silently wipe (same idea as LO's
        # delete-header prompt). Clear via page_set first, then turn off.
        blocked = _disable_blocked_by_content(doc, style, kwargs)
        if blocked:
            return self._tool_error(blocked)

        # type/enum checks are done by the tool schema in ToolRegistry
        # Convert before any UNO write. _mm_to_hmm and _page_style_layout_to_uno still
        # raise on negatives and layouts outside 0..3; execute_safe codes that.
        conversions: dict[str, Any] = {}
        if "is_landscape" in kwargs:
            conversions["is_landscape"] = kwargs["is_landscape"]

        for kwarg, _label, _uno_prop, kind in _PAGE_SET_SPECS:
            if kwarg not in kwargs:
                continue
            val = kwargs[kwarg]
            if kind == "mm":
                conversions[kwarg] = _mm_to_hmm(val)
            elif kind == "layout":
                conversions[kwarg] = _page_style_layout_to_uno(val)
            else:
                conversions[kwarg] = val

        updated: list[str] = []

        # is_landscape is applied first. If width/height were not passed, restore
        # the snapshotted dimensions so the setter doesn't swap them silently.
        if "is_landscape" in conversions:
            try:
                w_snap = style.getPropertyValue("Width")
                h_snap = style.getPropertyValue("Height")
                style.setPropertyValue("IsLandscape", conversions["is_landscape"])
                if "width_mm" not in conversions:
                    style.setPropertyValue("Width", w_snap)
                if "height_mm" not in conversions:
                    style.setPropertyValue("Height", h_snap)
                updated.append("is_landscape")
            except Exception as e:
                if is_disposed_exception(e):
                    raise
                return {
                    "status": "error",
                    "style_name": style_name,
                    "updated": updated,
                    "failed": {"is_landscape": str(e)},
                    "message": f"Failed to set is_landscape on page style '{style_name}': {e}. Nothing was changed.",
                }

        # Write remaining properties in spec order, stopping at first failure.
        for kwarg, label, uno_prop, _kind in _PAGE_SET_SPECS:
            if kwarg in conversions:
                try:
                    style.setPropertyValue(uno_prop, conversions[kwarg])
                    updated.append(label)
                except Exception as e:
                    if is_disposed_exception(e):
                        raise
                    failed_dict = {label: str(e)}
                    if not updated:
                        msg = f"Failed to set {label} on page style '{style_name}': {e}. Nothing was changed."
                    else:
                        msg = f"Failed to set {label} on page style '{style_name}': {e}. Earlier properties ({', '.join(updated)}) stay applied."
                    return {
                        "status": "error",
                        "style_name": style_name,
                        "updated": updated,
                        "failed": failed_dict,
                        "message": msg,
                    }

        result = {"status": "ok", "style_name": style_name, "updated": updated}
        flag_style_unreviewed(getattr(ctx, "ctx", None), result)
        return result


# ------------------------------------------------------------------
# PageGetHeaderFooterText
# ------------------------------------------------------------------


class PageGetHeaderFooterText(ToolWriterPageBase):
    """Get a page-style header or footer as HTML (same export as the body)."""

    name: str | None = "page_get_header_footer_text"
    description: str = (
        "Get this page-style header or footer as HTML so you can edit structure "
        "(fields, tables, logos) and send it back to page_set_header_footer_text. "
        "Uses the same XHTML export as get_document_content, pointed at the region's "
        "XText. Also lists images, fields, and paragraph_count so structure is visible "
        "without parsing the HTML. Use header_first / footer_first when first_is_shared is false."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "style": {
                "type": "string",
                "description": "The name of the page style (e.g., 'Standard' or 'Default Style'). Defaults to 'Standard'.",
            },
            "region": {
                "type": "string",
                "enum": list(_REGIONS),
                "description": (
                    "Which region to read. 'header'/'footer' are the shared ones. Use the "
                    "'_first' variants for a 'different first page' letterhead and the '_left' "
                    "variants when left/right pages differ (see first_is_shared / header_is_shared "
                    "in page_get_style_properties)."),
            },
            "include_images": {
                "type": "boolean",
                "description": (
                    "Include embedded logo/image bytes in the HTML (default true). "
                    "Headers are small; omitting images would hide the letterhead the "
                    "caller is about to edit."),
            },
        },
        "required": ["region"],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from .html_export import xtext_to_content

        style_name = kwargs.get("style", "Standard")
        region = cast("str", kwargs.get("region"))
        include_images = bool(kwargs.get("include_images", True))
        # type/enum checks are done by the tool schema in ToolRegistry

        try:
            style, style_name = resolve_page_style(ctx.doc, style_name)
        except ValueError as e:
            return self._tool_error(str(e))

        is_on_prop, text_prop = _REGION_PROPS[region]
        is_on = bool(style.getPropertyValue(is_on_prop))
        # F5: disabling the region *clears* HeaderText — typically None
        # while off. Still export if a text object is present.
        text_obj = None
        try:
            text_obj = style.getPropertyValue(text_prop)
        except Exception:
            text_obj = None
        content = ""
        if text_obj is not None:
            content = xtext_to_content(
                text_obj, ctx.doc, ctx.ctx, getattr(ctx, "services", None),
                include_images=include_images,
            )
        result: dict[str, Any] = {
            "status": "ok",
            "style_name": style_name,
            "region": region,
            "content": content,
            "is_on": is_on,
            "format": "html",
        }
        if text_obj is not None:
            # Machine-readable extras; HTML already has the structure. Not a write refuse gate.
            scan = _scan_region_content(ctx.doc, text_obj)
            result["paragraph_count"] = scan["paragraph_count"]
            if scan["fields"]:
                result["fields"] = scan["fields"]
            if scan["images"]:
                result["images"] = scan["images"]
            if scan.get("warning"):
                result["warning"] = scan["warning"]
        dyn_prop, _unused_spacing, height_prop = _height_props(region)
        try:
            result["auto_height"] = bool(style.getPropertyValue(dyn_prop))
            result["height_mm"] = round(style.getPropertyValue(height_prop) / 100.0, 1)
        except Exception:
            pass
        return result


# ------------------------------------------------------------------
# PageSetHeaderFooterText
# ------------------------------------------------------------------


class PageSetHeaderFooterText(ToolWriterPageBase):
    """Replace a page-style header or footer with HTML (same import as the body)."""

    name: str | None = "page_set_header_footer_text"
    description: str = (
        "Replace this page-style header or footer with HTML so logos, tables, and "
        "page-number fields survive — the same StarWriter import as apply_document_content, "
        "pointed at the region's XText. Call page_get_header_footer_text first and edit "
        "that HTML. Enables the region if it is off. Pass auto_height=true so a taller "
        "letterhead grows instead of overlapping the body. Plain text is wrapped as a "
        "paragraph and still goes through import. A _first or _left region that only mirrors "
        "the shared header or footer is skipped; edit header/footer or turn sharing off. "
        "To change one substring and leave fields and floating graphics alone, use "
        "page_replace_header_footer_text."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "style": {
                "type": "string",
                "description": "The name of the page style (e.g., 'Standard' or 'Default Style'). Defaults to 'Standard'.",
            },
            "region": {
                "type": "string",
                "enum": list(_REGIONS),
                "description": (
                    "Which region to set. 'header'/'footer' are the shared ones; '_first' targets a "
                    "'different first page' letterhead and '_left' the left-hand pages."),
            },
            "content": {
                "type": "string",
                "description": (
                    "HTML (or plain text) to put in the region. Prefer the HTML returned by "
                    "page_get_header_footer_text so fields are <span title=\"page-number\"/> "
                    "and images are <img> — those round-trip."),
            },
            "auto_height": {
                "type": "boolean",
                "description": (
                    "Let the region grow with its content so taller content is not clipped "
                    "and does not overlap the body. Left unchanged when omitted."
                ),
            },
        },
        "required": ["region", "content"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from .html_import import replace_xtext_with_html

        style_name = kwargs.get("style", "Standard")
        region = cast("str", kwargs.get("region"))
        content = kwargs.get("content", "")

        # type/enum checks are done by the tool schema in ToolRegistry

        try:
            style, style_name = resolve_page_style(ctx.doc, style_name)
        except ValueError as e:
            return self._tool_error(str(e))

        # Mirrors cannot be set independently without turning sharing off first.
        if _region_mirrors_shared(style, region):
            return _mirrored_region_skip(style_name, region)

        is_on_prop, text_prop = _REGION_PROPS[region]
        was_on = False
        try:
            was_on = style.getPropertyValue(is_on_prop) is True
        except Exception:
            was_on = False

        auto_height = kwargs.get("auto_height")
        old_auto_height = None
        if auto_height is not None:
            dyn_prop, _spacing_prop, _height_prop = _height_props(region)
            try:
                old_auto_height = bool(style.getPropertyValue(dyn_prop))
            except Exception:
                old_auto_height = None

        try:
            style.setPropertyValue(is_on_prop, True)
            if auto_height is not None:
                set_header_footer_auto_height(style, region, auto_height)

            text_obj = style.getPropertyValue(text_prop)
            if not text_obj:
                raise RuntimeError(f"Could not retrieve text object for {region} on style '{style_name}'.")

            config_svc = None
            services = getattr(ctx, "services", None)
            if services is not None:
                try:
                    config_svc = services.get("config")
                except Exception:
                    config_svc = None
            note = replace_xtext_with_html(
                text_obj, content, config_svc=config_svc, model=ctx.doc,
            )
            result: dict[str, Any] = {
                "status": "ok",
                "style_name": style_name,
                "region": region,
                "updated": True,
                "format": "html",
            }
            if note:
                result["warning"] = note
            if auto_height is not None:
                result["auto_height"] = bool(auto_height)
            flag_style_unreviewed(getattr(ctx, "ctx", None), result)
            return result
        except Exception as e:
            if is_disposed_exception(e):
                raise
            # What was wrong: a failed HTML import left a previously disabled region half-enabled.
            # How it happened: is_on was set True before replace_xtext_with_html was called.
            # Why this change fixes it: roll back is_on to False if it wasn't on before, or restore auto_height.
            if not was_on:
                try:
                    style.setPropertyValue(is_on_prop, False)
                except Exception:
                    pass
            elif auto_height is not None and old_auto_height is not None:
                try:
                    set_header_footer_auto_height(style, region, old_auto_height)
                except Exception:
                    pass
            return self._tool_error(f"Error writing to {region} text on page style '{style_name}': {e}")


# A field is one cursor step, but getString() expands it to the digits the reader
# sees. goRight follows the cursor step, so the search string is built from portions
# (\x01 per TextField), not from getString(). doc.findFirst is the wrong tool here
# too: it hits the body first, and it cannot see a header on a page style no page uses.
_FIELD_CURSOR_STEP = "\x01"


def _portion_cursor_text(portion: Any) -> str:
    """Text this portion contributes to a goRight index. A field is one step."""
    try:
        kind = portion.getPropertyValue("TextPortionType")
    except Exception:
        return ""
    if kind == "TextField":
        return _FIELD_CURSOR_STEP
    try:
        chunk = portion.getString()
    except Exception:
        return ""
    return chunk if isinstance(chunk, str) else ""


def _paragraph_cursor_text(para: Any) -> str | None:
    """Concatenate portion steps for one paragraph. None when it cannot be walked."""
    try:
        portions = para.createEnumeration()
    except Exception:
        return None
    parts: list[str] = []
    seen = 0
    while portions.hasMoreElements() is True and seen < _SCAN_PORTION_LIMIT:
        seen += 1
        try:
            portion = portions.nextElement()
        except Exception:
            break
        parts.append(_portion_cursor_text(portion))
    record_walk_cap(portions, seen, _SCAN_PORTION_LIMIT, "text portions", None)
    return "".join(parts)


def _lower_keeps_length(text: str) -> bool:
    folded = text.lower()
    return len(folded) == len(text)


def _match_offsets(hay: str, needle: str, case_sensitive: bool) -> list[int]:
    """Non-overlapping start indexes of *needle* in *hay*.

    Case-insensitive search uses lower() only when that does not change length,
    so the index still matches goRight. A length-changing fold falls back to
    an exact match.
    """
    if not needle:
        return []
    if case_sensitive or not (_lower_keeps_length(hay) and _lower_keeps_length(needle)):
        src, pat = hay, needle
    else:
        src, pat = hay.lower(), needle.lower()
    step = len(needle)
    hits: list[int] = []
    start = 0
    while True:
        found = src.find(pat, start)
        if found < 0:
            break
        hits.append(found)
        start = found + step
    return hits


def _same_region_text(selected: Any, needle: str, case_sensitive: bool) -> bool:
    """True when the cursor selection is the requested match."""
    if not isinstance(selected, str):
        return False
    if case_sensitive or not (_lower_keeps_length(selected) and _lower_keeps_length(needle)):
        return selected == needle
    return selected.lower() == needle.lower()


def _find_region_matches(text_obj: Any, needle: str, case_sensitive: bool) -> tuple[list[tuple[Any, int, int]], list[str]]:
    """Return (paragraph, offset, length) hits and any walk-cap notes.

    The offset and length count UTF-16 code units from the paragraph start. A match may span
    portions (mixed bold). It does not span a paragraph break.
    """
    hits: list[tuple[Any, int, int]] = []
    notes: list[str] = []
    try:
        enum = text_obj.createEnumeration()
    except Exception:
        return hits, notes
    seen = 0
    while enum.hasMoreElements() is True and seen < _SCAN_PARA_LIMIT:
        seen += 1
        try:
            para = enum.nextElement()
        except Exception:
            break
        hay = _paragraph_cursor_text(para)
        if hay is None:
            continue
        for offset in _match_offsets(hay, needle, case_sensitive):
            # What was wrong: Python string indexing counts Unicode code points, but
            # LibreOffice XText cursor positions count UTF-16 code units. Non-BMP chars
            # (like emojis) caused goRight to land short and fail the match.
            # Why this change fixes it: measure uno_offset and uno_length via _uno_len.
            uno_offset = _uno_len(hay[:offset])
            uno_length = _uno_len(hay[offset:offset + len(needle)])
            hits.append((para, uno_offset, uno_length))
    record_walk_cap(enum, seen, _SCAN_PARA_LIMIT, "paragraphs", notes)
    return hits, notes


def _cursor_for_match(text_obj: Any, para: Any, offset: int, length: int) -> Any | None:
    """Collapsed cursor grown to the match. None when goRight cannot land there."""
    try:
        cursor = text_obj.createTextCursorByRange(para.getStart())
    except Exception:
        return None

    # What was wrong: in LibreOffice Writer, XTextCursor.goRight steps by Unicode code points
    # (characters), but _find_region_matches measures UTF-16 code units (uno_offset and uno_length).
    # Non-BMP characters (like emojis) have length 2 in UTF-16 but count as 1 character in goRight,
    # causing goRight to overshoot and getString() to mismatch the needle.
    # Why this change fixes it: if the paragraph text is available, convert UTF-16 counts to code points.
    go_offset = offset
    go_length = length
    hay = _paragraph_cursor_text(para)
    if hay:
        u16_cur = 0
        char_offset = None
        char_end = None
        target_end = offset + length
        for char_idx, ch in enumerate(hay):
            if char_offset is None and u16_cur >= offset:
                char_offset = char_idx
            if char_end is None and u16_cur >= target_end:
                char_end = char_idx
                break
            u16_cur += 2 if ord(ch) > 0xFFFF else 1
        if char_offset is None and u16_cur >= offset:
            char_offset = len(hay)
        if char_end is None and u16_cur >= target_end:
            char_end = len(hay)
        if char_offset is not None and char_end is not None:
            go_offset = char_offset
            go_length = char_end - char_offset

    try:
        # `is False`: a MagicMock goRight() is truthy and must not abort the walk.
        if go_offset and cursor.goRight(go_offset, False) is False:
            return None
        if cursor.goRight(go_length, True) is False:
            return None
    except Exception:
        return None
    return cursor


def _point_before(text: Any, left: Any, right: Any) -> bool:
    """True when *left* starts before *right* (compareRegionStarts == 1)."""
    try:
        return int(text.compareRegionStarts(left, right)) == 1
    except Exception:
        return False


def _anchor_point_in_match(text: Any, anchor: Any, cursor: Any) -> bool:
    """True when a collapsed anchor's start lies inside [cursor.start, cursor.end)."""
    try:
        a_start = anchor.getStart()
        m_start = cursor.getStart()
        m_end = cursor.getEnd()
    except Exception:
        return False
    return not _point_before(text, a_start, m_start) and _point_before(text, a_start, m_end)


def _character_anchor_in_matches(doc: Any, text_obj: Any, cursors: list[Any]) -> str | None:
    """Name of an AT_CHARACTER shape whose anchor lies inside a match, if any.

    Probe A: setString on footer text left an AT_PARAGRAPH graphic and its
    HyperLinkURL alone. An AT_CHARACTER anchor sits in the text range, so
    setString there can drop the image. Refuse before writing.
    """
    from com.sun.star.text.TextContentAnchorType import AT_CHARACTER

    try:
        draw_page = doc.getDrawPage()
        count = int(draw_page.getCount())
    except Exception:
        return None
    shapes: list[Any] = []
    for i in range(min(count, _SCAN_SHAPE_LIMIT)):
        try:
            shapes.append(draw_page.getByIndex(i))
        except Exception:
            break
    for shape in shapes:
        try:
            anchor = shape.getAnchor()
            if not uno_same(anchor.getText(), text_obj):
                continue
        except Exception:
            continue
        try:
            if shape.getPropertyValue("AnchorType") != AT_CHARACTER:
                continue
        except Exception:
            continue
        for cursor in cursors:
            if _anchor_point_in_match(text_obj, anchor, cursor):
                return _shape_name(shape)
    return None


def _apply_match_cursors(doc: Any, text_obj: Any, cursors: list[Any], new_text: str) -> tuple[str | None, int, int]:
    """setString from the last match backward. Returns (error, fields, shapes).

    WriterCompoundUndo only groups; it does not undo a failed write. Several
    matches have to leave the context and undo that title, same as a TOC
    one-entry edit, or a later failure would leave the earlier replacements.
    """
    from .edit_review import next_agent_edit_undo_title

    before = _scan_region_content(doc, text_obj)
    after = before
    title = next_agent_edit_undo_title()
    mgr = None
    try:
        candidate = doc.getUndoManager()
        # `is True`: MagicMock isLocked() is truthy and must not look locked.
        if candidate is not None and candidate.isLocked() is not True:
            candidate.enterUndoContext(title)
            mgr = candidate
    except Exception:
        log.warning("header/footer replace: undo grouping skipped", exc_info=True)
        mgr = None
    if mgr is None and len(cursors) > 1:
        return "Cannot replace every match in one undo step. Nothing was changed.", 0, 0

    error: str | None = None
    try:
        for cursor in reversed(cursors):
            cursor.setString(new_text)
        after = _scan_region_content(doc, text_obj)
        if len(after["fields"]) < len(before["fields"]) or len(after["images"]) < len(before["images"]):
            raise RuntimeError("the replace removed a field or graphic")
    except Exception as exc:
        error = str(exc)
    left = False
    undone = False
    if mgr is not None:
        try:
            mgr.leaveUndoContext()
            left = True
        except Exception:
            log.exception("header/footer replace: leaveUndoContext failed")
            error = error or "Could not close the header/footer undo context."
        if error and left:
            try:
                titles = mgr.getAllUndoActionTitles()
                if titles and titles[0] == title:
                    mgr.undo()
                    undone = True
            except Exception:
                log.exception("header/footer replace rollback failed")
    if error:
        # What was wrong: if a post-check failed or replacement error occurred but undo() did not run,
        # the error message did not warn that edits were already written.
        # Why this change fixes it: prefix with note that replacement could not be rolled back.
        if not undone:
            error = "the replacement was written and could not be rolled back: " + error
        return error, 0, 0
    return None, len(after["fields"]), len(after["images"])


class PageReplaceHeaderFooterText(ToolWriterPageBase):
    """Replace one substring in a header or footer without rewriting the region."""

    name: str | None = "page_replace_header_footer_text"
    description: str = (
        "Replace one substring in this page-style header or footer. Leaves page-number "
        "fields and floating graphics in place. One old_content/content pair per call — "
        "call again for a second change, the same way apply_document_content search-replace "
        "does. Does not turn the region on and does not rewrite it; use "
        "page_set_header_footer_text for a full HTML replacement. A region that only mirrors "
        "the shared header or footer is skipped. Refuses when an image is anchored to a "
        "character inside the match."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "style": {
                "type": "string",
                "description": "Page style name. Defaults to 'Standard'.",
            },
            "region": {
                "type": "string",
                "enum": list(_REGIONS),
                "description": (
                    "Which region to edit. 'header'/'footer' are the shared ones. "
                    "'_first' and '_left' are skipped when they only mirror the shared region."
                ),
            },
            "old_content": {
                "type": "string",
                "description": "Exact substring to find in the region's text. Does not match inside a field.",
            },
            "content": {
                "type": "string",
                "description": "Text to write in place of old_content. Empty deletes the match.",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Case-sensitive match (default true).",
            },
            "occurrence": {
                "type": "integer",
                "minimum": 0,
                "description": "0-based match to replace (default 0). Cannot be combined with all_matches=true.",
            },
            "all_matches": {
                "type": "boolean",
                "description": "Replace every match (default false). Cannot be combined with occurrence.",
            },
        },
        "required": ["region", "old_content", "content"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        style_name = kwargs.get("style", "Standard")
        region = cast("str", kwargs.get("region"))
        old_content = cast("str", kwargs.get("old_content"))
        new_text = cast("str", kwargs.get("content"))
        # type/enum checks are done by the tool schema in ToolRegistry
        if old_content == "":
            return self._tool_error("old_content must be a non-empty string.")
        if _FIELD_CURSOR_STEP in old_content:
            return self._tool_error("old_content cannot include a field.")
        case_sensitive = kwargs.get("case_sensitive", True)
        all_matches = kwargs.get("all_matches", False)
        raw_occurrence = kwargs.get("occurrence", None)
        if all_matches and raw_occurrence is not None:
            return self._tool_error("occurrence cannot be combined with all_matches=true.")
        occurrence = 0
        if raw_occurrence is not None:
            if raw_occurrence < 0:
                return self._tool_error("occurrence must be a non-negative integer.")
            occurrence = raw_occurrence

        try:
            style, style_name = resolve_page_style(ctx.doc, style_name)
        except ValueError as e:
            return self._tool_error(str(e))

        # A shared mirror is the same text as header/footer. Editing it would
        # surprise the caller who asked for the first or left page only.
        if _region_mirrors_shared(style, region):
            return _mirrored_region_skip(style_name, region)

        is_on_prop, text_prop = _REGION_PROPS[region]
        try:
            is_on = style.getPropertyValue(is_on_prop) is True
        except Exception:
            is_on = False
        if not is_on:
            return self._tool_error(
                "The %s is off on page style '%s'. Turn it on before replacing text."
                % (region, style_name)
            )
        try:
            text_obj = style.getPropertyValue(text_prop)
        except Exception:
            text_obj = None
        if text_obj is None:
            return self._tool_error(
                "Could not retrieve text object for %s on style '%s'." % (region, style_name)
            )

        hits, notes = _find_region_matches(text_obj, old_content, case_sensitive)
        if not hits:
            message = "'%s' was not found in the %s of page style '%s'." % (old_content, region, style_name)
            if notes:
                message = "%s %s" % (message, notes[0])
            return self._tool_error(message)
        if all_matches:
            chosen = hits
        elif occurrence >= len(hits):
            return self._tool_error(
                "'%s' has %d match(es) in the %s; occurrence %d is past the end. Nothing was changed."
                % (old_content, len(hits), region, occurrence)
            )
        else:
            chosen = [hits[occurrence]]

        cursors: list[Any] = []
        for para, offset, length in chosen:
            cursor = _cursor_for_match(text_obj, para, offset, length)
            if cursor is None:
                return self._tool_error(
                    "Could not select '%s' in the %s. Nothing was changed." % (old_content, region)
                )
            try:
                selected = cursor.getString()
            except Exception:
                selected = None
            # The portion index and the cursor must name the same characters.
            # A miss here means a field was counted wrong; do not setString.
            if not _same_region_text(selected, old_content, case_sensitive):
                return self._tool_error(
                    "The match for '%s' did not line up with the text (a field may sit inside it). "
                    "Nothing was changed." % old_content
                )
            cursors.append(cursor)

        blocked = _character_anchor_in_matches(ctx.doc, text_obj, cursors)
        if blocked:
            return self._tool_error(
                "An image (%s) is anchored to a character inside '%s', so replacing it would drop "
                "the image. Nothing was changed." % (blocked, old_content)
            )

        error, fields_preserved, shapes_preserved = _apply_match_cursors(
            ctx.doc, text_obj, cursors, new_text,
        )
        if error:
            return self._tool_error(
                "Could not replace '%s' in the %s: %s" % (old_content, region, error)
            )
        result: dict[str, Any] = {
            "status": "ok",
            "style": style_name,
            "region": region,
            "replaced_count": len(cursors),
            "fields_preserved": fields_preserved,
            "shapes_preserved": shapes_preserved,
        }
        if notes:
            result["warning"] = notes[0]
        flag_style_unreviewed(getattr(ctx, "ctx", None), result)
        return result


# ------------------------------------------------------------------
# PageGetColumns
# ------------------------------------------------------------------


class PageGetColumns(ToolWriterPageBase):
    """Get the column layout for a page style."""

    name: str | None = "page_get_columns"
    description: str = "Get the column layout for a page style. Column widths are relative weights, not millimetres. Margins are millimetres."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"style": {"type": "string", "description": "The name of the page style. Defaults to 'Standard'."}}, "required": []}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        style_name = kwargs.get("style", "Standard")
        doc = ctx.doc

        try:
            style, style_name = resolve_page_style(doc, style_name)
        except ValueError as e:
            return self._tool_error(str(e))

        text_columns = style.getPropertyValue("TextColumns")
        if not text_columns:
            return self._tool_error(f"TextColumns property not found on style '{style_name}'.")

        column_count = text_columns.getColumnCount()
        cols = text_columns.getColumns()

        columns_data = []
        for col in cols:
            columns_data.append({
                "width": col.Width,
                "width_unit": "relative",
                "left_margin_mm": col.LeftMargin / 100.0,
                "right_margin_mm": col.RightMargin / 100.0,
            })

        return {"status": "ok", "style": style_name, "style_name": style_name, "column_count": column_count, "columns": columns_data}


# ------------------------------------------------------------------
# PageSetColumns
# ------------------------------------------------------------------


class PageSetColumns(ToolWriterPageBase):
    """Set the number of columns and spacing for a page style."""

    name: str | None = "page_set_columns"
    description: str = "Set the number of columns and spacing for a page style."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "style": {"type": "string", "description": "The name of the page style. Defaults to 'Standard'."},
            "column_count": {"type": "integer", "description": "Number of columns (e.g., 2)."},
            "spacing_mm": {"type": "number", "description": "Spacing between columns in mm. Defaults to 0."},
        },
        "required": ["column_count"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        style_name = kwargs.get("style", "Standard")
        column_count = cast("int", kwargs.get("column_count"))
        spacing_raw = kwargs.get("spacing_mm", 0)

        # type/enum checks are done by the tool schema in ToolRegistry
        if column_count < 1:
            return self._tool_error("column_count must be at least 1.")

        spacing_hmm = _mm_to_hmm(spacing_raw)

        doc = ctx.doc

        try:
            style, style_name = resolve_page_style(doc, style_name)
        except ValueError as e:
            return self._tool_error(str(e))

        text_columns = style.getPropertyValue("TextColumns")
        if not text_columns:
            return self._tool_error(f"TextColumns property not found on style '{style_name}'.")

        text_columns.setColumnCount(column_count)
        cols = list(text_columns.getColumns())

        # Divide spacing between adjacent columns
        # Column 1 right margin gets half spacing, Column 2 left margin gets half, etc.
        # What was wrong: when spacing was 0, the inner margins were skipped leaving old spacing intact.
        # Why this change fixes it: always update inner margins for column_count > 1, setting them to 0 if spacing is 0.
        if column_count > 1:
            half_spacing = spacing_hmm // 2
            for i in range(column_count - 1):
                cols[i].RightMargin = half_spacing
                cols[i + 1].LeftMargin = half_spacing

        text_columns.setColumns(tuple(cols))
        style.setPropertyValue("TextColumns", text_columns)

        result: dict[str, Any] = {
            "status": "ok",
            "style_name": style_name,
            "column_count": column_count,
            "spacing_mm": spacing_raw,
        }
        flag_style_unreviewed(getattr(ctx, "ctx", None), result)
        return result


def _page_break_style_request(doc: Any, kwargs: dict[str, Any]) -> tuple[str | None, int | None]:
    """Return (resolved page style or None, restart offset or None).

    Does not modify the document. Unknown page_style raises the ValueError
    from resolve_page_style. PageNumberOffset is a short, so values above
    32767 are rejected here, before BreakType is set.
    """
    restart = kwargs.get("restart_page_number", None)
    style_name = kwargs.get("page_style", None)

    if restart is not None:
        if isinstance(restart, bool) or not isinstance(restart, int) or restart < 1 or restart > 32767:
            raise ValueError("restart_page_number must be an integer >= 1.")
        if not isinstance(style_name, str) or style_name == "":
            raise ValueError("restart_page_number requires page_style.")

    if style_name is None:
        return None, None
    if not isinstance(style_name, str):
        raise ValueError("page_style must be a string.")

    _style, resolved = resolve_page_style(doc, style_name)
    return resolved, restart


def _apply_page_break(para: Any, page_style: str | None, restart: int | None) -> None:
    """Set BreakType, then PageDescName, then PageNumberOffset.

    PageDescName is how Writer switches page style at a break
    (css.style.ParagraphProperties: setting it also requests a page break
    and names the style). BreakType is still set: that is the existing
    page_insert_break write. PageNumberOffset is a short; 0 means continue
    the current sequence, so this function does not write 0. The offset is
    written after PageDescName. Nothing is read back.
    """
    from com.sun.star.style.BreakType import PAGE_BEFORE
    para.setPropertyValue("BreakType", PAGE_BEFORE)
    if page_style is not None:
        para.setPropertyValue("PageDescName", page_style)
    if restart is not None:
        para.setPropertyValue("PageNumberOffset", restart)


def _page_break_ok(message: str, page_style: str | None, restart: int | None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "ok", "message": message}
    if page_style is not None:
        result["page_style"] = page_style
    if restart is not None:
        result["restart_page_number"] = restart
    return result


# ------------------------------------------------------------------
# PageInsertBreak
# ------------------------------------------------------------------


class PageInsertBreak(ToolWriterPageBase):
    """Insert a page break at a text anchor (before_text/after_text) or at the view cursor.

    Optional page_style applies that existing page style from the new page
    (PageDescName). Optional restart_page_number (with page_style) restarts
    numbering there (PageNumberOffset).
    """

    name: str | None = "page_insert_break"
    description: str = (
        "Start a new page. With no anchor, breaks at the user's cursor (arbitrary over MCP). "
        "Pass before_text or after_text to break the page at a specific passage instead, so a "
        "headless client can place it deterministically (e.g. before_text of the signature block). "
        "Pass page_style to apply an existing page style from that new page onward "
        "(often the name returned by style_clone of the current page, for an annex). "
        "Pass restart_page_number (integer >= 1) only together with page_style to restart "
        "page numbering at that value. An unknown page_style is an error and changes nothing."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {
        "before_text": {"type": "string", "description": "Break so this passage starts a new page (page break on the match's paragraph)."},
        "after_text": {"type": "string", "description": "Break the page right after this passage's paragraph."},
        "occurrence": {"type": "integer", "description": "0-based match to use when the anchor text repeats (default 0)."},
        "case_sensitive": {"type": "boolean", "description": "Case-sensitive anchor match (default true)."},
        "page_style": {
            "type": "string",
            "description": (
                "Name of an existing page style to apply from this break onward "
                "(paragraph property PageDescName). Often a style_clone of the current page. "
                "Unknown names error and change nothing. Omit to keep the current page style."
            ),
        },
        "restart_page_number": {
            "type": "integer",
            "description": (
                "Page number to start at on the new page (integer >= 1, paragraph property "
                "PageNumberOffset). Requires page_style. Omit to continue the current sequence. "
                "0 is not a restart."
            ),
        },
    }, "required": []}
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        before_text = kwargs.get("before_text")
        after_text = kwargs.get("after_text")
        if before_text and after_text:
            return self._tool_error("Pass only one of before_text / after_text.")

        try:
            page_style, restart = _page_break_style_request(doc, kwargs)
        except ValueError as e:
            return self._tool_error(str(e))

        if before_text or after_text:
            anchor = str(before_text or after_text)
            # type/enum checks are done by the tool schema in ToolRegistry
            occurrence = kwargs.get("occurrence", 0)
            if occurrence < 0:
                return self._tool_error("occurrence must be non-negative.")
            sd = doc.createSearchDescriptor()
            sd.SearchString = anchor
            sd.SearchRegularExpression = False
            sd.SearchCaseSensitive = bool(kwargs.get("case_sensitive", True))
            found = doc.findFirst(sd)
            for _unused in range(occurrence):
                if found is None:
                    break
                found = doc.findNext(found.getEnd(), sd)
            if found is None:
                return self._tool_error("Anchor text '%s' not found%s." % (anchor, (" at occurrence %d" % occurrence) if occurrence else ""))
            text = found.getText()
            # BreakType is a PARAGRAPH property. before_text -> the match's own paragraph;
            # after_text -> the paragraph that FOLLOWS the match.
            para = text.createTextCursorByRange(found.getStart() if before_text else found.getEnd())
            if after_text and not para.gotoNextParagraph(False):
                # At the LAST paragraph there is nothing after the anchor; silently breaking
                # on the anchor's own paragraph would push the anchor itself to a new page
                # (the opposite of what was asked) while reporting ok.
                return self._tool_error(
                    "The anchor is in the document's last paragraph; there is nothing after it "
                    "to start a new page with. Use before_text, or append content first.")
            # What was wrong: setting BreakType inside a table cell corrupted table layout or had no effect.
            # Why this change fixes it: refuse to set BreakType if the cursor is inside a text table.
            if _in_text_table(para):
                return self._tool_error("Page breaks are ignored inside a table cell. Nothing was changed.")
            _apply_page_break(para, page_style, restart)
            return _page_break_ok("Page break inserted %s the anchor." % ("before" if before_text else "after"), page_style, restart)

        view_cursor = doc.getCurrentController().getViewCursor()
        if not view_cursor:
            return self._tool_error("Could not obtain view cursor.")

        # BreakType is a paragraph property. A collapsed model cursor at the
        # view start applies it to the paragraph that contains the selection.
        # SwXTextViewCursor is not an XParagraphCursor (no gotoEndOfParagraph)
        # and this path must not move that selection. insertControlCharacter
        # (PARAGRAPH_BREAK) split the paragraph and destroyed the selected
        # text; do not put that back.
        text = view_cursor.getText()
        cursor = text.createTextCursorByRange(view_cursor.getStart())
        if _in_text_table(cursor):
            return self._tool_error("Page breaks are ignored inside a table cell. Nothing was changed.")
        _apply_page_break(cursor, page_style, restart)
        return _page_break_ok("Page break inserted.", page_style, restart)


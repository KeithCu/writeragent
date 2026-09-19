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

from typing import Any

from plugin.framework.errors import make_tool_error
from plugin.framework.uno_context import uno_same
from .format import record_walk_cap
from .specialized_base import ToolWriterPageBase

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


def _region_has_table(text_obj) -> bool:
    """True when the region's enumeration includes a text table (letterhead grid)."""
    try:
        enum = text_obj.createEnumeration()
    except Exception:
        return False
    seen = 0
    while enum.hasMoreElements() is True and seen < _SCAN_PARA_LIMIT:
        seen += 1
        try:
            el = enum.nextElement()
        except Exception:
            break
        try:
            # `is True`: MagicMock supportsService() is truthy and would false-positive.
            if el.supportsService("com.sun.star.text.TextTable") is True:
                return True
        except Exception:
            continue
    return False


def _region_holds_content(doc, text_obj) -> bool:
    """True if the header/footer XText still holds text, fields, images, or tables.

    F5: disabling the region (``HeaderIsOn`` / ``FooterIsOn`` = false) *clears*
    its content. A ``None`` text object is the usual off-state absence. The
    #646 refuse uses this so ``*_is_on=false`` does not silently wipe — same
    idea as LibreOffice's delete-header prompt. Clear via ``page_set`` first,
    then turn off.
    ``getString()`` misses logos and can hide tables; the #638 region scan plus a
    table walk covers those.
    """
    if text_obj is None:
        return False
    try:
        plain = text_obj.getString()
    except Exception:
        plain = ""
    if isinstance(plain, str) and plain.strip():
        return True
    scan = _scan_region_content(doc, text_obj)
    if scan["fields"] or scan["images"]:
        return True
    return _region_has_table(text_obj)


def _region_mirrors_shared(style, region: str) -> bool:
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


def _disable_blocked_by_content(doc, style, kwargs: dict[str, Any]) -> str | None:
    """Error text if ``header_is_on=false`` / ``footer_is_on=false`` would wipe content.

    F5: turning the region off *clears* its content (not "LO keeps HeaderText").
    Refuse ``*_is_on=false`` while content remains so we don't silently wipe —
    same idea as LibreOffice's delete-header prompt. Clear via ``page_set``
    first, then turn off.

    ``HeaderIsOn`` / ``FooterIsOn`` are the only toggles (no first/left IsOn props).
    Turning one off drops every variant of that kind, so independent matching
    regions are scanned. Shared-mirror first/left variants are skipped — they
    are the same XText when the matching ``*IsShared`` flag is on. Enabling
    (``true``) is never blocked. No force-off flag.
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
            except Exception:
                continue
            if _region_holds_content(doc, text_obj):
                held.append(region)
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


def _scan_region_content(doc, text_obj) -> dict[str, Any]:
    """Report fields and anchored images in a header/footer as get-side extras.

    HTML ``content`` already carries structure; these lists are machine-readable so a
    caller can see logos and live fields without parsing the markup. Returns
    ``{"fields": [...], "images": [...], "paragraph_count": int}``.
    """
    out = _empty_scan()
    try:
        para_enum = text_obj.createEnumeration()
    except Exception:
        return out
    notes: list[str] = []
    # `is True` and the hard caps match the enumeration walk in format.py: a UNO enumeration that
    # misbehaves otherwise spins here forever.
    while para_enum.hasMoreElements() is True and out["paragraph_count"] < _SCAN_PARA_LIMIT:
        try:
            para = para_enum.nextElement()
        except Exception:
            break
        out["paragraph_count"] += 1
        try:
            portions = para.createEnumeration()
        except Exception:
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
                if not uno_same(shape.getAnchor().getText(), text_obj):
                    continue
            except Exception:
                continue
            try:
                out["images"].append(str(shape.getName()) or "unnamed")
            except Exception:
                out["images"].append("unnamed")
    except Exception:
        pass
    return out


def _height_props(region: str) -> tuple[str, str, str]:
    """Return the (dynamic-height, dynamic-spacing, height) property names."""
    prefix = "Header" if _region_kind(region) == "header" else "Footer"
    return (prefix + "IsDynamicHeight", prefix + "DynamicSpacing", prefix + "Height")


def set_header_footer_auto_height(style, region: str, enabled: bool) -> None:
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


def resolve_page_style(doc, style_name: str = "Standard"):
    """Return ``(style_object, resolved_name)`` for a Writer page style."""
    styles = doc.getStyleFamilies().getByName("PageStyles")
    if not styles.hasByName(style_name):
        available = list(styles.getElementNames())
        raise ValueError("No page style named '%s'. Available: %s" % (style_name, ", ".join(available)))
    return styles.getByName(style_name), style_name


def get_page_style_properties(doc, style_name: str = "Standard") -> dict[str, Any]:
    """Read dimensions, margins, and header/footer state of a Writer page style.

    Shared by ``page_get_style_properties`` and ``style_get_info(family=PageStyles)``.
    A future Option B could hard-merge those two public APIs into one (winner TBD:
    general ``style_get_info`` vs the page toolkit); deferred for now.
    """
    try:
        style_families = doc.getStyleFamilies()
        page_styles = style_families.getByName("PageStyles")
        if not page_styles.hasByName(style_name):
            return make_tool_error(f"Page style '{style_name}' not found.")
        style = page_styles.getByName(style_name)
    except Exception as e:
        return make_tool_error(f"Error accessing page style '{style_name}': {e}")

    try:
        props = {
            "style_name": style_name,
            "width_mm": style.getPropertyValue("Width") / 100.0,
            "height_mm": style.getPropertyValue("Height") / 100.0,
            "is_landscape": style.getPropertyValue("IsLandscape"),
            "left_margin_mm": style.getPropertyValue("LeftMargin") / 100.0,
            "right_margin_mm": style.getPropertyValue("RightMargin") / 100.0,
            "top_margin_mm": style.getPropertyValue("TopMargin") / 100.0,
            "bottom_margin_mm": style.getPropertyValue("BottomMargin") / 100.0,
            "gutter_margin_mm": style.getPropertyValue("GutterMargin") / 100.0,
            "header_is_on": style.getPropertyValue("HeaderIsOn"),
            "footer_is_on": style.getPropertyValue("FooterIsOn"),
            "header_is_shared": style.getPropertyValue("HeaderIsShared"),
            "footer_is_shared": style.getPropertyValue("FooterIsShared"),
            "header_height_mm": style.getPropertyValue("HeaderHeight") / 100.0,
            "footer_height_mm": style.getPropertyValue("FooterHeight") / 100.0,
            "header_body_distance_mm": style.getPropertyValue("HeaderBodyDistance") / 100.0,
            "footer_body_distance_mm": style.getPropertyValue("FooterBodyDistance") / 100.0,
            "back_color": style.getPropertyValue("BackColor"),
            "back_transparent": style.getPropertyValue("BackTransparent"),
            "numbering_type": style.getPropertyValue("NumberingType"),
            "footnote_height_mm": style.getPropertyValue("FootnoteHeight") / 100.0,
            "register_paragraph_style": style.getPropertyValue("RegisterParagraphStyle"),
        }
        # False means the first page has its OWN header/footer — the usual setup for a
        # letterhead — reachable only through the header_first / footer_first regions. Fetched
        # separately: not every page style offers it, and one missing property must not sink
        # the whole read.
        try:
            props["first_is_shared"] = style.getPropertyValue("FirstIsShared")
        except Exception:
            pass
        # Attempt to safely fetch PageStyleLayout enum
        try:
            psl = style.getPropertyValue("PageStyleLayout")
            props["page_style_layout"] = psl.value if hasattr(psl, "value") else int(psl)
        except Exception:
            pass
        return {"status": "ok", "properties": props}
    except Exception as e:
        return make_tool_error(f"Error reading properties from page style '{style_name}': {e}")


# ------------------------------------------------------------------
# PageGetStyleProperties
# ------------------------------------------------------------------


class PageGetStyleProperties(ToolWriterPageBase):
    """Get dimensions, margins, and header/footer states of a page style."""

    name = "page_get_style_properties"
    description = "Get dimensions, margins, and header/footer states of a page style."
    parameters = {"type": "object", "properties": {"style": {"type": "string", "description": "The name of the page style (e.g., 'Standard' or 'Default Style'). Defaults to 'Standard'."}}, "required": []}

    def execute(self, ctx, **kwargs):
        return get_page_style_properties(ctx.doc, kwargs.get("style", "Standard"))


# ------------------------------------------------------------------
# PageSetStyleProperties
# ------------------------------------------------------------------


class PageSetStyleProperties(ToolWriterPageBase):
    """Modify dimensions, margins, and header/footer toggles of a page style."""

    name = "page_set_style_properties"
    description = (
        "Modify dimensions, margins, and header/footer toggles of a page style. "
        "header_is_on=false / footer_is_on=false refuse if that region still has "
        "text, fields, images, or tables — clear with page_set_header_footer_text "
        "first, then disable. Enabling (true) is always allowed. No force-off."
    )
    parameters = {
        "type": "object",
        "properties": {
            "style": {"type": "string", "description": "The name of the page style (e.g., 'Standard' or 'Default Style'). Defaults to 'Standard'."},
            "width_mm": {"type": "number", "description": "New width in mm."},
            "height_mm": {"type": "number", "description": "New height in mm."},
            "is_landscape": {"type": "boolean", "description": "Set orientation to landscape."},
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
            "numbering_type": {"type": "integer", "description": "Numbering type enum (4=Arabic, 0=Roman)."},
            "footnote_height_mm": {"type": "number", "description": "Max footnote area height in mm."},
            "register_paragraph_style": {"type": "string", "description": "Register true reference style."},
            "page_style_layout": {"type": "integer", "description": "0=ALL, 1=LEFT, 2=RIGHT, 3=MIRRORED"},
        },
        "required": [],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        style_name = kwargs.get("style", "Standard")
        doc = ctx.doc

        try:
            style_families = doc.getStyleFamilies()
            page_styles = style_families.getByName("PageStyles")
            if not page_styles.hasByName(style_name):
                return self._tool_error(f"Page style '{style_name}' not found.")
            style = page_styles.getByName(style_name)
        except Exception as e:
            return self._tool_error(f"Error accessing page style '{style_name}': {e}")

        # F5: HeaderIsOn/FooterIsOn=false *clears* content. Refuse while
        # content remains so we don't silently wipe (same idea as LO's
        # delete-header prompt). Clear via page_set first, then turn off.
        blocked = _disable_blocked_by_content(doc, style, kwargs)
        if blocked:
            return self._tool_error(blocked)

        updated = []
        try:
            if "width_mm" in kwargs:
                style.setPropertyValue("Width", int(kwargs["width_mm"] * 100))
                updated.append("width")
            if "height_mm" in kwargs:
                style.setPropertyValue("Height", int(kwargs["height_mm"] * 100))
                updated.append("height")
            if "is_landscape" in kwargs:
                style.setPropertyValue("IsLandscape", kwargs["is_landscape"])
                updated.append("is_landscape")
            if "left_margin_mm" in kwargs:
                style.setPropertyValue("LeftMargin", int(kwargs["left_margin_mm"] * 100))
                updated.append("left_margin")
            if "right_margin_mm" in kwargs:
                style.setPropertyValue("RightMargin", int(kwargs["right_margin_mm"] * 100))
                updated.append("right_margin")
            if "top_margin_mm" in kwargs:
                style.setPropertyValue("TopMargin", int(kwargs["top_margin_mm"] * 100))
                updated.append("top_margin")
            if "bottom_margin_mm" in kwargs:
                style.setPropertyValue("BottomMargin", int(kwargs["bottom_margin_mm"] * 100))
                updated.append("bottom_margin")
            if "gutter_margin_mm" in kwargs:
                style.setPropertyValue("GutterMargin", int(kwargs["gutter_margin_mm"] * 100))
                updated.append("gutter_margin")
            if "header_is_on" in kwargs:
                style.setPropertyValue("HeaderIsOn", kwargs["header_is_on"])
                updated.append("header_is_on")
            if "footer_is_on" in kwargs:
                style.setPropertyValue("FooterIsOn", kwargs["footer_is_on"])
                updated.append("footer_is_on")
            if "header_is_shared" in kwargs:
                style.setPropertyValue("HeaderIsShared", kwargs["header_is_shared"])
                updated.append("header_is_shared")
            if "footer_is_shared" in kwargs:
                style.setPropertyValue("FooterIsShared", kwargs["footer_is_shared"])
                updated.append("footer_is_shared")
            if "first_is_shared" in kwargs:
                style.setPropertyValue("FirstIsShared", kwargs["first_is_shared"])
                updated.append("first_is_shared")
            if "header_height_mm" in kwargs:
                style.setPropertyValue("HeaderHeight", int(kwargs["header_height_mm"] * 100))
                updated.append("header_height")
            if "footer_height_mm" in kwargs:
                style.setPropertyValue("FooterHeight", int(kwargs["footer_height_mm"] * 100))
                updated.append("footer_height")
            if "header_body_distance_mm" in kwargs:
                style.setPropertyValue("HeaderBodyDistance", int(kwargs["header_body_distance_mm"] * 100))
                updated.append("header_body_distance")
            if "footer_body_distance_mm" in kwargs:
                style.setPropertyValue("FooterBodyDistance", int(kwargs["footer_body_distance_mm"] * 100))
                updated.append("footer_body_distance")
            if "back_color" in kwargs:
                style.setPropertyValue("BackColor", kwargs["back_color"])
                updated.append("back_color")
            if "back_transparent" in kwargs:
                style.setPropertyValue("BackTransparent", kwargs["back_transparent"])
                updated.append("back_transparent")
            if "numbering_type" in kwargs:
                style.setPropertyValue("NumberingType", kwargs["numbering_type"])
                updated.append("numbering_type")
            if "footnote_height_mm" in kwargs:
                style.setPropertyValue("FootnoteHeight", int(kwargs["footnote_height_mm"] * 100))
                updated.append("footnote_height")
            if "register_paragraph_style" in kwargs:
                style.setPropertyValue("RegisterParagraphStyle", kwargs["register_paragraph_style"])
                updated.append("register_paragraph_style")
            if "page_style_layout" in kwargs:
                from com.sun.star.style.PageStyleLayout import ALL, LEFT, RIGHT, MIRRORED

                m = {0: ALL, 1: LEFT, 2: RIGHT, 3: MIRRORED}
                val = m.get(kwargs["page_style_layout"])
                if val is not None:
                    style.setPropertyValue("PageStyleLayout", val)
                    updated.append("page_style_layout")
        except Exception as e:
            return self._tool_error(f"Error setting properties on page style '{style_name}': {e}")

        return {"status": "ok", "style_name": style_name, "updated": updated}


# ------------------------------------------------------------------
# PageGetHeaderFooterText
# ------------------------------------------------------------------


class PageGetHeaderFooterText(ToolWriterPageBase):
    """Get a page-style header or footer as HTML (same export as the body)."""

    name = "page_get_header_footer_text"
    description = (
        "Get this page-style header or footer as HTML so you can edit structure "
        "(fields, tables, logos) and send it back to page_set_header_footer_text. "
        "Uses the same XHTML export as get_document_content, pointed at the region's "
        "XText. Also lists images, fields, and paragraph_count so structure is visible "
        "without parsing the HTML. Use header_first / footer_first when first_is_shared is false."
    )
    parameters = {
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

    def execute(self, ctx, **kwargs):
        from .html_export import xtext_to_content

        style_name = kwargs.get("style", "Standard")
        region = kwargs.get("region")
        include_images = bool(kwargs.get("include_images", True))
        if region not in _REGION_PROPS:
            return self._tool_error("region is required, one of: %s." % ", ".join(_REGIONS))

        try:
            style, style_name = resolve_page_style(ctx.doc, style_name)
        except Exception as e:
            return self._tool_error(f"Error accessing page style '{style_name}': {e}")

        try:
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
        except Exception as e:
            return self._tool_error(f"Error reading {region} text from page style '{style_name}': {e}")


# ------------------------------------------------------------------
# PageSetHeaderFooterText
# ------------------------------------------------------------------


class PageSetHeaderFooterText(ToolWriterPageBase):
    """Replace a page-style header or footer with HTML (same import as the body)."""

    name = "page_set_header_footer_text"
    description = (
        "Replace this page-style header or footer with HTML so logos, tables, and "
        "page-number fields survive — the same StarWriter import as apply_document_content, "
        "pointed at the region's XText. Call page_get_header_footer_text first and edit "
        "that HTML. Enables the region if it is off. Pass auto_height=true so a taller "
        "letterhead grows instead of overlapping the body. Plain text is wrapped as a "
        "paragraph and still goes through import."
    )
    parameters = {
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
    is_mutation = True

    def execute(self, ctx, **kwargs):
        from .html_import import replace_xtext_with_html

        style_name = kwargs.get("style", "Standard")
        region = kwargs.get("region")
        content = kwargs.get("content", "")

        if region not in _REGION_PROPS:
            return self._tool_error("region is required, one of: %s." % ", ".join(_REGIONS))

        try:
            style, style_name = resolve_page_style(ctx.doc, style_name)
        except Exception as e:
            return self._tool_error(f"Error accessing page style '{style_name}': {e}")

        try:
            is_on_prop, text_prop = _REGION_PROPS[region]
            style.setPropertyValue(is_on_prop, True)
            auto_height = kwargs.get("auto_height")
            if auto_height is not None:
                set_header_footer_auto_height(style, region, auto_height)

            text_obj = style.getPropertyValue(text_prop)
            if not text_obj:
                return self._tool_error(f"Could not retrieve text object for {region} on style '{style_name}'.")

            config_svc = None
            services = getattr(ctx, "services", None)
            if services is not None:
                try:
                    config_svc = services.get("config")
                except Exception:
                    config_svc = None
            replace_xtext_with_html(
                text_obj, content, config_svc=config_svc, model=ctx.doc,
            )
            # Windows findFirst can miss a just-written header until idle
            # (GHA 35466498641 test_search_still_reaches_header_after_html_set).
            try:
                from plugin.framework.uno_context import get_toolkit

                toolkit = get_toolkit(getattr(ctx, "ctx", None))
                if toolkit is not None:
                    toolkit.processEventsToIdle()
            except Exception:
                pass
            result: dict[str, Any] = {
                "status": "ok",
                "style_name": style_name,
                "region": region,
                "updated": True,
                "format": "html",
            }
            if auto_height is not None:
                result["auto_height"] = bool(auto_height)
            return result
        except Exception as e:
            return self._tool_error(f"Error writing to {region} text on page style '{style_name}': {e}")


# ------------------------------------------------------------------
# PageGetColumns
# ------------------------------------------------------------------


class PageGetColumns(ToolWriterPageBase):
    """Get the column layout for a page style."""

    name = "page_get_columns"
    description = "Get the column layout for a page style."
    parameters = {"type": "object", "properties": {"style": {"type": "string", "description": "The name of the page style. Defaults to 'Standard'."}}, "required": []}

    def execute(self, ctx, **kwargs):
        style_name = kwargs.get("style", "Standard")
        doc = ctx.doc

        try:
            style_families = doc.getStyleFamilies()
            page_styles = style_families.getByName("PageStyles")
            if not page_styles.hasByName(style_name):
                return self._tool_error(f"Page style '{style_name}' not found.")
            style = page_styles.getByName(style_name)
        except Exception as e:
            return self._tool_error(f"Error accessing page style '{style_name}': {e}")

        try:
            text_columns = style.getPropertyValue("TextColumns")
            if not text_columns:
                return self._tool_error(f"TextColumns property not found on style '{style_name}'.")

            column_count = text_columns.getColumnCount()
            cols = text_columns.getColumns()

            columns_data = []
            for col in cols:
                columns_data.append({"width": col.Width, "left_margin_mm": col.LeftMargin / 100.0, "right_margin_mm": col.RightMargin / 100.0})

            return {"status": "ok", "style": style_name, "style_name": style_name, "column_count": column_count, "columns": columns_data}
        except Exception as e:
            return self._tool_error(f"Error reading columns from page style '{style_name}': {e}")


# ------------------------------------------------------------------
# PageSetColumns
# ------------------------------------------------------------------


class PageSetColumns(ToolWriterPageBase):
    """Set the number of columns and spacing for a page style."""

    name = "page_set_columns"
    description = "Set the number of columns and spacing for a page style."
    parameters = {
        "type": "object",
        "properties": {
            "style": {"type": "string", "description": "The name of the page style. Defaults to 'Standard'."},
            "column_count": {"type": "integer", "description": "Number of columns (e.g., 2)."},
            "spacing_mm": {"type": "number", "description": "Spacing between columns in mm. Defaults to 0."},
        },
        "required": ["column_count"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        style_name = kwargs.get("style", "Standard")
        column_count = kwargs.get("column_count")
        spacing_mm = kwargs.get("spacing_mm", 0)

        if column_count is None or column_count < 1:
            return self._tool_error("column_count must be at least 1.")

        doc = ctx.doc

        try:
            style_families = doc.getStyleFamilies()
            page_styles = style_families.getByName("PageStyles")
            if not page_styles.hasByName(style_name):
                return self._tool_error(f"Page style '{style_name}' not found.")
            style = page_styles.getByName(style_name)
        except Exception as e:
            return self._tool_error(f"Error accessing page style '{style_name}': {e}")

        try:
            text_columns = style.getPropertyValue("TextColumns")
            if not text_columns:
                return self._tool_error(f"TextColumns property not found on style '{style_name}'.")

            text_columns.setColumnCount(column_count)
            cols = list(text_columns.getColumns())

            spacing = int(spacing_mm * 100)

            # Divide spacing between adjacent columns
            # Column 1 right margin gets half spacing, Column 2 left margin gets half, etc.
            if column_count > 1 and spacing > 0:
                half_spacing = spacing // 2
                for i in range(column_count - 1):
                    cols[i].RightMargin = half_spacing
                    cols[i + 1].LeftMargin = half_spacing

            text_columns.setColumns(tuple(cols))
            style.setPropertyValue("TextColumns", text_columns)

            return {"status": "ok", "style_name": style_name, "column_count": column_count, "spacing_mm": spacing_mm}
        except Exception as e:
            return self._tool_error(f"Error setting columns on page style '{style_name}': {e}")


# ------------------------------------------------------------------
# PageInsertBreak
# ------------------------------------------------------------------


class PageInsertBreak(ToolWriterPageBase):
    """Insert a page break at a text anchor (before_text/after_text) or at the view cursor."""

    name = "page_insert_break"
    description = (
        "Start a new page. With no anchor, breaks at the user's cursor (arbitrary over MCP). "
        "Pass before_text or after_text to break the page at a specific passage instead, so a "
        "headless client can place it deterministically (e.g. before_text of the signature block)."
    )
    parameters = {"type": "object", "properties": {
        "before_text": {"type": "string", "description": "Break so this passage starts a new page (page break on the match's paragraph)."},
        "after_text": {"type": "string", "description": "Break the page right after this passage's paragraph."},
        "occurrence": {"type": "integer", "description": "0-based match to use when the anchor text repeats (default 0)."},
        "case_sensitive": {"type": "boolean", "description": "Case-sensitive anchor match (default true)."},
    }, "required": []}
    is_mutation = True

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        before_text = kwargs.get("before_text")
        after_text = kwargs.get("after_text")
        if before_text and after_text:
            return self._tool_error("Pass only one of before_text / after_text.")

        try:
            if before_text or after_text:
                anchor = str(before_text or after_text)
                try:
                    occurrence = int(kwargs.get("occurrence", 0) or 0)
                except (TypeError, ValueError):
                    return self._tool_error("occurrence must be an integer.")
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
                from com.sun.star.style.BreakType import PAGE_BEFORE
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
                para.setPropertyValue("BreakType", PAGE_BEFORE)
                return {"status": "ok", "message": "Page break inserted %s the anchor." % ("before" if before_text else "after")}

            view_cursor = doc.getCurrentController().getViewCursor()
            if not view_cursor:
                return self._tool_error("Could not obtain view cursor.")

            from com.sun.star.style.BreakType import PAGE_BEFORE
            text = view_cursor.getText()
            cursor = text.createTextCursorByRange(view_cursor)
            cursor.setPropertyValue("BreakType", PAGE_BEFORE)
            # Optionally insert a paragraph break so the break actually applies cleanly
            text.insertControlCharacter(cursor, 0, False)  # 0 = PARAGRAPH_BREAK
            return {"status": "ok", "message": "Page break inserted."}
        except Exception as e:
            return self._tool_error(f"Error inserting page break: {e}")

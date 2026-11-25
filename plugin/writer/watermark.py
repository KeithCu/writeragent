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
"""Writer watermark tools (page domain, specialized tier).

Drives LibreOffice's own ``.uno:Watermark`` dispatch and status query
(``sw/source/uibase/app/docsh2.cxx`` ``SwDocShell::Execute`` case ``SID_WATERMARK``,
``sw/source/core/edit/edfcol.cxx`` ``SwEditShell::SetWatermark`` / ``GetWatermark``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

import unohelper
from com.sun.star.frame import XStatusListener

from plugin.doc.compound_undo import UndoLock
from plugin.doc.visual_helpers import color_param, parse_color_to_uno_int
from plugin.framework.errors import best_effort, is_disposal, uno_read
from plugin.framework.uno_context import create_property_value, execute_dispatch, resolve_uno_context
from .edit_review import flag_style_unreviewed
from .search import iter_draw_shapes
from .specialized_base import ToolWriterPageBase

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

log = logging.getLogger("writeragent.writer")

WATERMARK_COMMAND = ".uno:Watermark"
PICTURE_WATERMARK_PREFIX = "WordPictureWatermark"  # oox/source/export/vmlexport.cxx IsWaterMarkShape
TEXT_WATERMARK_PREFIX = "PowerPlusWaterMarkObject"  # sw/source/core/edit/edfcol.cxx WATERMARK_NAME
# What lcl_placeWatermarkInHeader (sw/source/core/edit/edfcol.cxx) derives from the watermark's
# font, color, transparency and angle. Transformation also carries the size fitted to the text.
_WATERMARK_LOOK_PROPS = (
    "CharFontName",
    "CharFontNameAsian",
    "CharFontNameComplex",
    "FillColor",
    "FillTransparence",
    "Transformation",
)


class _WatermarkStateListener(unohelper.Base, XStatusListener):  # type: ignore[misc]
    """Collects the synchronous FeatureStateEvent sent by ``addStatusListener``."""

    def __init__(self) -> None:
        self.event: Any = None

    def statusChanged(self, State: Any) -> None:
        self.event = State

    def disposing(self, Source: Any) -> None:
        pass


def read_watermark(uno_ctx: Any, doc: Any) -> dict[str, Any] | None:
    """Read LibreOffice's ``SwEditShell::GetWatermark`` via the ``.uno:Watermark`` status.

    Returns ``{"text": str, "font": str, "angle": int, "transparency": int, "color": int}``
    (``text == ""`` means no text watermark; other fields are then LibreOffice defaults),
    or ``None`` when the document has no controller/frame/dispatch, the command is disabled
    (e.g. read-only document), or ``State`` is not a ``PropertyValue`` tuple.
    """
    if uno_ctx is None or doc is None:
        return None
    controller = doc.getCurrentController()
    if controller is None:
        return None
    frame = controller.getFrame()
    if frame is None:
        return None

    import uno

    url = cast("Any", uno.createUnoStruct("com.sun.star.util.URL"))
    url.Complete = WATERMARK_COMMAND
    smgr = uno_ctx.getServiceManager()
    transformer = smgr.createInstanceWithContext("com.sun.star.util.URLTransformer", uno_ctx)
    if transformer is None:
        return None
    # SfxBaseController::queryDispatch matches on aURL.Protocol == ".uno:", which requires parseStrict.
    parsed, url = transformer.parseStrict(url)
    if not parsed:
        return None

    dispatch = frame.queryDispatch(url, "", 0)
    if dispatch is None:
        return None

    listener = _WatermarkStateListener()
    # SfxDispatchController_Impl::addStatusListener calls QueryState and delivers
    # statusChanged synchronously (sfx2/source/control/unoctitm.cxx).
    dispatch.addStatusListener(listener, url)
    try:
        event = listener.event
    finally:
        dispatch.removeStatusListener(listener, url)

    if event is None or not event.IsEnabled:
        return None
    state = event.State
    if not isinstance(state, tuple):
        return None

    # SwDocShell::StateStyleSheet (sw/source/uibase/app/docst.cxx) packs SfxWatermarkItem into State.
    raw = {pv.Name: pv.Value for pv in state if pv.Name}
    if not {"Text", "Font", "Angle", "Transparency", "Color"}.issubset(raw):
        return None

    # edfcol.cxx lcl_GetAngle returns 360 for a 0-degree shape; normalize to 0..359.
    return {
        "text": str(raw["Text"]),
        "font": str(raw["Font"]),
        "angle": int(raw["Angle"]) % 360,
        "transparency": int(raw["Transparency"]),
        "color": int(raw["Color"]) & 0xFFFFFF,
    }


def _watermark_shapes(doc: Any, prefix: str) -> list[Any]:
    """Return draw-page shapes whose name starts with ``prefix``."""
    draw_page = uno_read(doc.getDrawPage, None)
    if draw_page is None:
        return []
    result: list[Any] = []
    for shape in iter_draw_shapes(draw_page):
        name = uno_read(shape.getName, "")
        if isinstance(name, str) and name.startswith(prefix):
            result.append(shape)
    return result


def _dispose_shapes(shapes: list[Any]) -> int:
    """Dispose draw-page shapes, re-raising document disposal and logging other errors."""
    removed = 0
    for shape in shapes:
        try:
            shape.dispose()
            removed += 1
        except Exception as exc:
            if is_disposal(exc):
                raise
            log.exception("Failed to dispose watermark shape")
    return removed


def _update_inactive_watermarks(doc: Any, text: str) -> None:
    """Copy the active watermark's text and look into watermark shapes in inactive headers.

    An inactive header's text belongs to no page descriptor format, so ``getAnchor().getText()``
    is ``None`` (``sw/source/core/unocore/unoobj2.cxx`` ``sw::CreateParentXText``). A
    page-anchored shape has no anchor at all (``getAnchor() is None``) and is left alone.
    The caller has verified through ``SwEditShell::GetWatermark`` that an active header holds
    this text, so a model exists whenever there are inactive copies.
    """
    inactive: list[Any] = []
    model: Any = None
    for shape in _watermark_shapes(doc, TEXT_WATERMARK_PREFIX):
        anchor = shape.getAnchor()
        if anchor is None:
            continue
        anchor_text = anchor.getText()
        if anchor_text is None:
            inactive.append(shape)
        elif (
            model is None
            and anchor_text.getImplementationName() == "SwXHeadFootText"
            and shape.getString() == text
        ):
            model = shape
    if not inactive:
        return
    active_model = cast("Any", model)
    with UndoLock(doc):
        for shape in inactive:
            shape.setString(text)
            for name in _WATERMARK_LOOK_PROPS:
                shape.setPropertyValue(name, active_model.getPropertyValue(name))


def _public_watermark(state: dict[str, Any]) -> dict[str, Any] | None:
    """Format an internal watermark state dict for tool output, or None if empty."""
    if not state["text"]:
        return None
    return {
        "text": state["text"],
        "font": state["font"],
        "angle": state["angle"],
        "transparency": state["transparency"],
        "color": f"#{state['color']:06x}",
    }


_UNSUPPORTED_MSG = (
    "The watermark command is not available for this document (read-only, or no Writer window)."
)


def _optional_int_in_range(kwargs: dict[str, Any], name: str, lo: int, hi: int) -> int | None | str:
    val = kwargs.get(name)
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, int) or not (lo <= val <= hi):
        return f"{name} must be an integer between {lo} and {hi}."
    return val


class PageGetWatermark(ToolWriterPageBase):
    """Read the current text watermark and whether a Word picture watermark exists."""

    name: str | None = "page_get_watermark"
    is_mutation: bool | None = False
    description: str = (
        "Read the document's text watermark (text, font, angle, transparency, color) "
        "and report whether a Word picture watermark exists."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        uno_ctx = resolve_uno_context(ctx)
        state = read_watermark(uno_ctx, ctx.doc)
        if state is None:
            return self._tool_error(_UNSUPPORTED_MSG, code="UNSUPPORTED")
        return {
            "status": "ok",
            "watermark": _public_watermark(state),
            "picture_watermark": bool(_watermark_shapes(ctx.doc, PICTURE_WATERMARK_PREFIX)),
        }


class PageSetWatermark(ToolWriterPageBase):
    """Add, update, or remove the document's text watermark via ``.uno:Watermark``."""

    name: str | None = "page_set_watermark"
    is_mutation: bool | None = True
    description: str = (
        "Set, update, or remove the document's text watermark (same as Format > Watermark). "
        "Omitted fields keep the current watermark's values, or LibreOffice defaults "
        "(Liberation Sans, angle 45, transparency 50, color #c0c0c0) when none exists. "
        "Font size is automatic (fitted to one line). Angle is anticlockwise degrees (0-359); "
        "Word's menu offers 45 (diagonal) and 0 (horizontal). Pass empty text ('') to remove "
        "the watermark. Setting or removing also deletes any Word picture watermark. "
        "Note: setting a watermark turns on the header of every page style. Setting or removing one clears the undo history."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Watermark text. Pass an empty string '' to remove the watermark.",
            },
            "font": {
                "type": "string",
                "description": "Font family name. Omit to keep current (default 'Liberation Sans').",
            },
            "angle": {
                "type": "integer",
                "minimum": 0,
                "maximum": 359,
                "description": "Anticlockwise rotation angle in degrees (0-359). Omit to keep current (default 45).",
            },
            "transparency": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": "Transparency percentage (0-100). Omit to keep current (default 50).",
            },
            "color": color_param(
                "Color as '#rrggbb', 'rgb(...)', or a color name. Omit to keep current (default '#c0c0c0')."
            ),
        },
        "required": ["text"],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        text = kwargs["text"]
        if not isinstance(text, str):
            return self._tool_error("text must be a string.")

        font_val: str | None = None
        if (raw_font := kwargs.get("font")) is not None:
            if not isinstance(raw_font, str) or not raw_font.strip():
                return self._tool_error("font must be a non-empty string.")
            font_val = raw_font.strip()

        angle_val = _optional_int_in_range(kwargs, "angle", 0, 359)
        if isinstance(angle_val, str):
            return self._tool_error(angle_val)

        tr_val = _optional_int_in_range(kwargs, "transparency", 0, 100)
        if isinstance(tr_val, str):
            return self._tool_error(tr_val)

        color_val: int | None = None
        if (raw_color := kwargs.get("color")) is not None:
            color_val = parse_color_to_uno_int(raw_color)
            if color_val is None:
                return self._tool_error(
                    f"Invalid color: {raw_color!r}. Expected '#rrggbb', 'rgb(...)', or a color name."
                )

        uno_ctx = resolve_uno_context(ctx)
        current = read_watermark(uno_ctx, ctx.doc)
        if current is None:
            return self._tool_error(_UNSUPPORTED_MSG, code="UNSUPPORTED")

        # Match SwWatermarkDialog::InitFields: omitted fields keep the current watermark's values.
        expected = {
            "text": text,
            "font": font_val if font_val is not None else current["font"],
            "angle": angle_val if angle_val is not None else current["angle"],
            "transparency": tr_val if tr_val is not None else current["transparency"],
            "color": color_val if color_val is not None else current["color"],
        }

        args = tuple(
            create_property_value(name, value)
            for name, value in (
                ("Text", expected["text"]),
                ("Font", expected["font"]),
                ("Angle", expected["angle"]),
                ("Transparency", expected["transparency"]),
                ("Color", expected["color"]),
            )
        )
        # Do not wrap in WriterCompoundUndo: SwXPageStyle::SetPropertyValues_Impl calls DelAllUndoObj()
        # on every page-style property write (i64460), so LibreOffice empties the undo stack itself.
        execute_dispatch(uno_ctx, ctx.doc.getCurrentController(), WATERMARK_COMMAND, args)

        after = read_watermark(uno_ctx, ctx.doc)
        if after is None:
            return self._tool_error("LibreOffice did not apply the watermark.")
        # On removal (text == ""), SwEditShell::GetWatermark returns the default SfxWatermarkItem
        # once no shape exists, so only text is verifiable.
        if text == "":
            if after["text"] != "":
                return self._tool_error("LibreOffice did not apply the watermark.")
        elif after != expected:
            return self._tool_error("LibreOffice did not apply the watermark.")

        # Word files can carry first-page and even-page headers that are switched off.
        # LibreOffice keeps them as stashed formats that HeaderTextFirst and HeaderTextLeft
        # do not return while shared (sw/source/core/unocore/unostyle.cxx
        # SwXPageStyle::GetPropertyValues_Impl), so SwEditShell::SetWatermark cannot reach them.
        # Without this step their watermark shapes stay stale: DOCX export writes them
        # (sw/source/filter/ww8/wrtw8sty.cxx MSWordExportBase::SectionProperties), and they come
        # back when the user turns on a different first or even page
        # (sw/source/core/doc/docdesc.cxx SwDoc::ChgPageDesc). On a verified removal any remaining
        # shape with this prefix is one of those copies; on a verified set, update inactive copies
        # in place with the active watermark's text and look.
        if text == "":
            if _dispose_shapes(_watermark_shapes(ctx.doc, TEXT_WATERMARK_PREFIX)):
                # Each dispose records a "Delete frame" undo entry, and one Undo would bring a
                # single stale copy back. Clear instead of locking undo around the dispose: when
                # every header is off the dispatch leaves older entries on the stack, and deleting
                # a frame with undo off leaves them pointing at freed memory
                # (SwUndoInsLayFormat, sw/source/core/undo/undobj1.cxx). clear() refuses while an
                # undo context is open (a streamed edit): the dispatch already marked the stack
                # for clearing when a header is on (SfxUndoManager::ClearAllLevels), whereas with
                # every header off the deletions stay in that open group.
                best_effort(ctx.doc.getUndoManager().clear, "clear undo after watermark removal failed")
        else:
            _update_inactive_watermarks(ctx.doc, text)

        # Word has one watermark per document, and DOCX export would write the picture next to
        # the new text, so Word would show both. Dispose any WordPictureWatermark* shapes.
        removed_pictures = _dispose_shapes(_watermark_shapes(ctx.doc, PICTURE_WATERMARK_PREFIX))

        result: dict[str, Any] = {
            "status": "ok",
            "watermark": _public_watermark(after),
            "picture_watermarks_removed": removed_pictures,
            "picture_watermark": bool(_watermark_shapes(ctx.doc, PICTURE_WATERMARK_PREFIX)),
        }
        flag_style_unreviewed(result, ctx.doc)
        return result

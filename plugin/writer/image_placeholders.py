# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Swap and restore data-less image placeholders across Writer HTML imports.

When get_document_content leaves image data out (include_images=false), pictures
come back as frame wrappers (<div ... id="Name"> / <span ... id="Name">) around
empty <img> tags. These helpers swap them for temporary markers before import and
restore or clean them up afterwards.
"""

# pyright: reportUnusedFunction=false

from __future__ import annotations

import contextlib
import html as html_mod
import logging
import re
import uuid
from typing import Any, Iterator, NamedTuple

from plugin.framework.errors import ToolExecutionError, reraise_if_disposed, uno_read

log = logging.getLogger("writeragent.writer")


def _is_recording_changes(model: Any) -> bool:
    """True if *model* is currently recording Track Changes (redlines).

    Agent edits made while recording must land as a clean tracked Delete + Insert so the
    user can accept (-> new text) or reject (-> old text) each one. The format-preserving
    replace paths (the char-by-char diff in ``replace_preserving_format`` and the
    paragraph-style restore below) corrupt that into a per-character mess or a FORMAT
    redline that keeps the old text on Accept -- so those steps are skipped while recording.
    """
    # RecordChanges does not fail on a live document. Returning False hid a
    # closed document and took the review-off path.
    return bool(model.getPropertyValue("RecordChanges"))


# --- images that come back without their data ------------------------------------------------
# get_document_content leaves image data out (include_images=false), so a picture comes back as
# its frame's wrapper -- <div ... id="Name"> for paragraph/character/page anchors, <span ...
# id="Name"> inside a line -- around <img src="">, and a floating frame is followed by a
# clear:both <div>. Imported as is, the picture was lost: the edit deleted the original with the
# old text and the import made nothing (a signature vanished on "Accept all"), and the clear:both
# <div> became an extra empty paragraph that shifted the next data-lo-style by one.
# A picture inside a heading comes back with a <p> between the wrapper and the <img>
# (<span id="Name"><p><img src=""/></p></span>); without that optional <p> the pattern missed
# it and the whole edit was refused as "an image without its data".
_IMAGE_PLACEHOLDER_RE = re.compile(
    r'<(div|span)\b[^>]*\bid="([^"]+)"[^>]*>\s*(?:<p\b[^>]*>\s*)?<img\b[^>]*\bsrc=""[^>]*>\s*(?:</p>\s*)?</\1>'
    r'(?:\s*<div style="clear:both;[^"]*">(?:\s|&nbsp;|\xa0)*</div>)?',
    re.IGNORECASE)
_EMPTY_IMG_RE = re.compile(r'<img\b[^>]*\bsrc=""', re.IGNORECASE)

# Frame properties copied onto the restored picture (read with try: not all exist on every frame).
_KEPT_IMAGE_PROPS = (
    "HoriOrient", "HoriOrientPosition", "HoriOrientRelation", "VertOrient", "VertOrientPosition",
    "VertOrientRelation", "TextWrap", "SurroundContour", "LeftMargin", "RightMargin", "TopMargin",
    "BottomMargin", "Title", "Description", "GraphicCrop", "Opaque",
)


class _KeptImage(NamedTuple):
    marker: str
    name: str
    original: Any
    graphic: Any
    anchor: Any
    size: Any
    props: dict[str, Any]


def _swap_image_placeholders(model: Any, content: str) -> tuple[str, list[_KeptImage]]:
    """Swap each data-less image wrapper that names a picture of *model* for a text marker.

    Runs BEFORE the edit deletes anything: the picture is read now, while it still exists (with
    review off, deleting its paragraph disposes of it). Raises ToolExecutionError -- nothing
    changed yet -- when an image has no data and names no picture here, instead of losing it.
    """
    if not content or 'src=""' not in content:
        return content, []
    graphics = uno_read(lambda: model.getGraphicObjects(), None)
    kept: list[_KeptImage] = []
    missing: list[str] = []
    nonce = uuid.uuid4().hex[:8]

    def _swap(match: re.Match[str]) -> str:
        name = html_mod.unescape(match.group(2))
        obj = uno_read(lambda: graphics.getByName(name) if graphics is not None and graphics.hasByName(name) else None, None)
        if obj is None:
            missing.append(name)
            return match.group(0)
        props: dict[str, Any] = {}
        for prop in _KEPT_IMAGE_PROPS:
            try:
                props[prop] = obj.getPropertyValue(prop)
            except Exception as exc:
                log.debug("_swap_image_placeholders: skip property %s: %s", prop, exc)
        marker = "wa%simg%d" % (nonce, len(kept))
        kept.append(_KeptImage(marker, name, obj, obj.getPropertyValue("Graphic"),
                               obj.getPropertyValue("AnchorType"), obj.getSize(), props))
        return marker

    swapped = _IMAGE_PLACEHOLDER_RE.sub(_swap, content)
    if missing or _EMPTY_IMG_RE.search(swapped):
        named = (" It names %s, which this document does not have." % ", ".join(repr(n) for n in missing)
                 if missing else "")
        raise ToolExecutionError(
            "The content has an image without its data (src=\"\"), so it would be lost.%s Keep the "
            "image's wrapper exactly as get_document_content returned it (<div ... id=\"Name\"> or "
            "<span ... id=\"Name\">) so the tool puts that picture back, leave the image out to "
            "delete it, or read with include_images=true to copy it from another document." % named)
    return swapped, kept


def _deleted_by_the_edit(model: Any, original: Any, name: str | None = None) -> bool:
    """True when the edit removed *original*: missing from getGraphicObjects() or disposed
    (review off), or its anchor -- for a paragraph anchor, the paragraph's break -- inside
    a pending tracked deletion (review on). Anything else is still in place and must not be
    duplicated, e.g. a search replace of the text of the paragraph a picture is anchored to."""
    # Bugfix: Calling original.getAnchor() crashes soffice with a null pointer dereference
    # inside SwXFrame::getSwAnchor (sw/source/core/unocore/unoframe.cxx) when a review-off
    # delete leaves SwXTextGraphicObject alive with an empty/null anchor. Check whether
    # doc.getGraphicObjects().hasByName(name) still has it before calling getAnchor(). If missing,
    # it was deleted by the edit so return True. Non-disposal lookup errors are logged and
    # treated as deleted (reraise_if_disposed first).
    if name:
        try:
            graphics = model.getGraphicObjects()
            if not graphics.hasByName(name):
                return True
        except Exception as exc:
            reraise_if_disposed(exc)
            log.warning("_deleted_by_the_edit: graphic object lookup failed for %r: %s", name, exc)
            return True
    try:
        anchor = original.getAnchor()
    except Exception as exc:
        reraise_if_disposed(exc)
        return True
    if not _is_recording_changes(model):
        return False
    point = anchor
    if getattr(original.getPropertyValue("AnchorType"), "value", "") == "AT_PARAGRAPH":
        # A paragraph-anchored picture goes only when its paragraph goes: when the break that
        # ends it is deleted. A search replace deletes the paragraph's text, not the paragraph.
        try:
            point = anchor.getText().createTextCursorByRange(anchor.getStart())
            point.gotoEndOfParagraph(False)
            if not point.goRight(1, True):
                return False  # the last paragraph has no break: it stays
        except Exception as exc:
            reraise_if_disposed(exc)
            return False
    try:
        redlines = model.getRedlines()
        total = int(redlines.getCount())
        enum = redlines.createEnumeration()
    except Exception as exc:
        reraise_if_disposed(exc)
        return False
    for _unused in range(total):
        if enum.hasMoreElements() is not True:
            break
        redline = enum.nextElement()
        try:
            if redline.getPropertyValue("RedlineType") != "Delete":
                continue
            start = redline.getPropertyValue("RedlineStart")
            end = redline.getPropertyValue("RedlineEnd")
            text = start.getText()
            # compareRegionStarts(a, b): 1 when a starts before b, 0 when equal.
            if text.compareRegionStarts(start, point) >= 0 and text.compareRegionEnds(point, end) >= 0:
                return True
        except Exception as exc:
            reraise_if_disposed(exc)
            continue  # another text object, or not comparable
    return False


def _restore_image_placeholders(model: Any, kept: list[_KeptImage]) -> None:
    """Put a copy of each kept picture where its marker landed, and remove the marker."""
    for image in kept:
        search = model.createSearchDescriptor()
        search.SearchString = image.marker
        search.SearchCaseSensitive = True
        found = model.findFirst(search)
        if found is None and hasattr(model, "getTextTables"):
            try:
                tables = model.getTextTables()
                for tbl_name in tables.getElementNames():
                    tbl = tables.getByName(tbl_name)
                    for cell_name in tbl.getCellNames():
                        cell = tbl.getCellByName(cell_name)
                        if hasattr(cell, "createSearchDescriptor"):
                            cell_found = cell.findFirst(search)
                            if cell_found is not None:
                                found = cell_found
                                break
                    if found is not None:
                        break
            except Exception as exc:
                reraise_if_disposed(exc)
        if found is None:
            if getattr(image.anchor, "value", "") != "AT_PAGE" and _deleted_by_the_edit(model, image.original, image.name):
                raise ToolExecutionError(f"Kept image {image.name!r} marker not found after import")
            log.warning("kept image %r: marker not found after the import", image.name)
            continue
        text = found.getText()
        if getattr(image.anchor, "value", "") == "AT_PAGE" or not _deleted_by_the_edit(model, image.original, image.name):
            # The original is still where it was (page-anchored, or outside what the edit
            # replaced): drop the marker instead of adding a duplicate.
            found.setString("")
            continue
        picture = model.createInstance("com.sun.star.text.TextGraphicObject")
        picture.Graphic = image.graphic
        picture.AnchorType = image.anchor
        picture.setSize(image.size)
        # Known LibreOffice limit (checked live, recorded or not): with review on, a picture in a
        # cell of a table this edit inserts keeps "Reject all" from removing that table. Accept is
        # right; Reject leaves the new table behind. Without the picture it would be lost instead.
        text.insertTextContent(found, picture, True)  # True: the marker text is replaced
        for prop, value in image.props.items():
            try:
                picture.setPropertyValue(prop, value)
            except Exception as exc:
                reraise_if_disposed(exc)
                log.debug("kept image %r: could not restore %s", image.name, prop, exc_info=True)
        try:
            picture.setName(image.name)  # taken while the original is a pending deletion
        except Exception as exc:
            reraise_if_disposed(exc)


@contextlib.contextmanager
def _keep_placeholder_images(model: Any, content: str) -> Iterator[str]:
    """Yield *content* with data-less images swapped for markers; put the pictures back after."""
    swapped, kept = _swap_image_placeholders(model, content)
    yield swapped
    if kept:
        _restore_image_placeholders(model, kept)


# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Swap and restore data-less image placeholders across Writer HTML imports.

When get_content leaves image data out (include_images=false), pictures
come back as frame wrappers (<div ... id="Name"> / <span ... id="Name">) around
empty <img> tags. These helpers swap them for temporary markers before import and
restore or clean them up afterwards.
An empty src written with single quotes or spaces is refused the same way, never passed through.
"""

from __future__ import annotations

import contextlib
import html as html_mod
import logging
import re
import uuid
from typing import Any, Generator, NamedTuple, NoReturn

from plugin.doc.text_helpers import TrackedDeletions
from plugin.framework.errors import ToolExecutionError, reraise_if_disposed, uno_read
from plugin.framework.uno_context import uno_same
from .range_copy import GRAPHIC_LAYOUT_PROPS, table_is_all_deleted
from .record_changes import is_recording_changes, record_changes

log = logging.getLogger("writeragent.writer")


# --- images that come back without their data ------------------------------------------------
# get_content leaves image data out (include_images=false), so a picture comes back as
# its frame's wrapper -- <div ... id="Name"> for paragraph/character/page anchors, <span ...
# id="Name"> inside a line -- around <img src="">, and a floating frame is followed by a
# clear:both <div>. An AT_PARAGRAPH image with a vertical offset is preceded by an emulation
# <div style="height:...">&nbsp;</div>, and a draw:frame comment with newlines sits before the
# wrapper. Imported as is, the picture would be lost: the edit deletes the original with the
# old text and the import makes nothing (a signature vanishes on "Accept all"), the
# emulation/clear:both <div> becomes an extra empty paragraph that shifts the next
# data-lo-style by one (or adds a \xa0 paragraph per round), and the draw:frame comment's
# newlines add a space mid-word. A picture inside a heading comes back with a <p> between
# the wrapper and the <img> (<span id="Name"><p><img src=""/></p></span>); the optional <p>
# in the pattern catches it, so the edit is not refused as "an image without its data".
# _FRAME_IMAGE_RE shares FRAME_HEAD for header/footer writes (data-less or with data),
# and html_export._FRAME_IMG_AHEAD shares it to match picture frames on export.
FRAME_HEAD = (
    r"(?:\n<!--Next 'div' is emulating the top height of a draw:frame\.-->\s*"
    r'(?:<div style="height:[^"]*">(?:\s|&nbsp;|\xa0)*</div>)?)?'
    r"(?:\n<!--Next '(?:div|span)' is a draw:frame\. -->\n)?"
)
_FRAME_TAIL = r'(?:\s*<div style="clear:both;[^"]*">(?:\s|&nbsp;|\xa0)*</div>)?'


def _frame_image_re(img: str) -> re.Pattern[str]:
    return re.compile(
        FRAME_HEAD
        + r'<(div|span)\b[^>]*\bid="([^"]+)"[^>]*>\s*(?:<p\b[^>]*>\s*)?'
        + img
        + r"\s*(?:</p>\s*)?</\1>"
        + _FRAME_TAIL,
        re.IGNORECASE,
    )


_IMAGE_PLACEHOLDER_RE = _frame_image_re(r'<img\b[^>]*\bsrc=""[^>]*>')
_FRAME_IMAGE_RE = _frame_image_re(r"(?P<img><img\b[^>]*>)")
_EMPTY_IMG_RE = re.compile(r'<img\b[^>]*\bsrc\s*=\s*(["\'])\s*\1', re.IGNORECASE)
_LINK_PROPS = ("HyperLinkURL", "HyperLinkName", "HyperLinkTarget")
_MARKER_PAT = r"wa[0-9a-f]{8}img\d+"
_MARKER_RE = re.compile(_MARKER_PAT)
# An <a href> holding only picture markers is the pictures' own link; one that also holds text is a
# text link the pictures sit in. The frame comment before <a> and the clear:both <div> after </a>
# stay outside when only the wrapper is linked, and are dropped with it.
_LINKED_MARKERS_RE = re.compile(
    FRAME_HEAD
    + rf"(<a\b(?=[^>]*\shref\s*=)[^>]*>)((?:\s*{_MARKER_PAT})+)(\s*)</a>"
    + _FRAME_TAIL,
    re.IGNORECASE,
)
# Same rule for region images after frame wrappers are unwrapped: an <a> holding only images is the pictures' own link.
_LINKED_REGION_IMGS_RE = re.compile(
    rf'<a\b(?=[^>]*\shref\s*=)[^>]*>(?:\s*<img\b[^>]*\bname="{_MARKER_PAT}"[^>]*>)+\s*</a>',
    re.IGNORECASE,
)
_1PX_PNG_DATA_URI = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

# Frame properties copied onto the restored picture (read with try: not all exist on every frame).
_KEPT_IMAGE_PROPS = GRAPHIC_LAYOUT_PROPS + _LINK_PROPS


class _KeptImage(NamedTuple):
    marker: str
    name: str
    original: Any
    graphic: Any
    anchor: Any
    size: Any
    props: dict[str, Any]
    linked: bool = False


class _RegionImage(NamedTuple):
    marker: str
    name: str
    kept: _KeptImage | None
    no_data: bool
    in_region: bool
    linked: bool = False


def _read_picture(obj: Any, marker: str, name: str) -> _KeptImage:
    props: dict[str, Any] = {}
    for prop in _KEPT_IMAGE_PROPS:
        try:
            props[prop] = obj.getPropertyValue(prop)
        except Exception as exc:
            log.debug("read_picture: skip property %s: %s", prop, exc)
    return _KeptImage(
        marker,
        name,
        obj,
        obj.getPropertyValue("Graphic"),
        obj.getPropertyValue("AnchorType"),
        obj.getSize(),
        props,
    )


def _refuse_lost_images(model: Any, missing: list[str]) -> NoReturn:
    embedded: list[str] = []
    unknown: list[str] = []
    if missing:
        objects = uno_read(lambda: model.getEmbeddedObjects(), None)
        for name in missing:
            is_embedded = bool(objects is not None and uno_read(lambda: objects.hasByName(name), False))
            (embedded if is_embedded else unknown).append(name)
    parts = ['The content has an image without its data (src=""), so it would be lost.']
    if embedded:
        parts.append(
            "It names %s, an embedded object (chart, spreadsheet, …) that a write cannot put back. "
            "Edit around it (target='search'), or leave its wrapper out to delete it."
            % ", ".join(repr(n) for n in embedded)
        )
    if unknown or not embedded:
        if unknown:
            parts.append("It names %s, which this document does not have." % ", ".join(repr(n) for n in unknown))
        parts.append(
            "Keep the image's wrapper exactly as get_content returned it (<div ... id=\"Name\"> or "
            "<span ... id=\"Name\">) so the tool puts that picture back, leave the image out to "
            "delete it, or read with include_images=true to copy it from another document. "
            "To link the picture, put the <a> around that wrapper."
        )
    raise ToolExecutionError(" ".join(parts))


def _merged_link(source: Any, kept: dict[str, Any]) -> dict[str, str]:
    """Merge imported link properties from *source* with *kept* fallback name and target.

    The imported marker's link wins: it is what the agent wrote, with relative URLs resolved.
    """
    link = {prop: str(source.getPropertyValue(prop) or "") for prop in _LINK_PROPS}
    if not link["HyperLinkURL"]:
        return {prop: "" for prop in _LINK_PROPS}
    # The XHTML export writes only the URL, so a missing name or target means "unchanged".
    return {prop: link[prop] or str(kept.get(prop) or "") for prop in _LINK_PROPS}


def _set_link(picture: Any, link: dict[str, str]) -> None:
    """Write only changed link properties onto *picture*.

    Applied untracked because LibreOffice does not track frame attributes; Undo reverts it
    and Reject All does not.
    """
    for prop, value in link.items():
        if str(picture.getPropertyValue(prop) or "") != value:
            picture.setPropertyValue(prop, value)


def _insert_copy(
    model: Any,
    text: Any,
    where: Any,
    *,
    name: str,
    graphic: Any,
    anchor: Any,
    size: Any,
    props: dict[str, Any],
    absorb: bool,
) -> Any:
    picture = model.createInstance("com.sun.star.text.TextGraphicObject")
    picture.Graphic = graphic
    picture.AnchorType = anchor
    picture.setSize(size)
    text.insertTextContent(where, picture, absorb)
    for prop, value in props.items():
        try:
            picture.setPropertyValue(prop, value)
        except Exception as exc:
            reraise_if_disposed(exc)
            log.debug("kept image %r: could not restore %s", name, prop, exc_info=True)
    try:
        picture.setName(name)  # taken while the original is a pending deletion
    except Exception as exc:
        reraise_if_disposed(exc)
    return picture


def _graphic_named(graphics: Any, name: str) -> Any:
    return uno_read(
        lambda: graphics.getByName(name) if graphics is not None and graphics.hasByName(name) else None,
        None,
    )


def capture_placeholder_images(model: Any, content: str) -> tuple[str, list[_KeptImage]]:
    """Swap each data-less image wrapper that names a picture of *model* for a text marker.

    Runs BEFORE the edit deletes anything: the picture is read now, while it still exists (with
    review off, deleting its paragraph disposes of it). Raises ToolExecutionError -- nothing
    changed yet -- when an image has no data and names no picture here, instead of losing it.
    """
    if not content or not _EMPTY_IMG_RE.search(content):
        return content, []
    graphics = uno_read(lambda: model.getGraphicObjects(), None)
    kept: list[_KeptImage] = []
    missing: list[str] = []
    nonce = uuid.uuid4().hex[:8]

    def _swap(match: re.Match[str]) -> str:
        name = html_mod.unescape(match.group(2))
        obj = _graphic_named(graphics, name)
        if obj is None:
            missing.append(name)
            return match.group(0)
        marker = "wa%simg%d" % (nonce, len(kept))
        kept.append(_read_picture(obj, marker, name))
        return marker

    swapped = _IMAGE_PLACEHOLDER_RE.sub(_swap, content)
    if missing or _EMPTY_IMG_RE.search(swapped):
        _refuse_lost_images(model, missing)
    linked: set[str] = set()

    def _note_link(match: re.Match[str]) -> str:
        linked.update(_MARKER_RE.findall(match.group(2)))
        trail = match.group(3)
        return match.group(1) + match.group(2) + "</a>" + ("" if "\n" in trail or "\r" in trail else trail)

    swapped = _LINKED_MARKERS_RE.sub(_note_link, swapped)
    return swapped, [k._replace(linked=k.marker in linked) for k in kept]


def capture_region_images(model: Any, text_obj: Any, html: str) -> tuple[str, list[_RegionImage]]:
    """Unwrap exported image frames in *html* and tag each ``<img>`` with a unique name.

    Runs BEFORE the region is cleared so data-less placeholders (``src=""``) can
    read their picture from *model* and unknown data-less images can be refused
    before anything changes.
    """
    if not html or "<img" not in html.lower():
        return html, []
    graphics = uno_read(lambda: model.getGraphicObjects(), None)
    images: list[_RegionImage] = []
    missing: list[str] = []
    nonce = uuid.uuid4().hex[:8]

    def _swap(match: re.Match[str]) -> str:
        name = html_mod.unescape(match.group(2))
        img = match.group("img")
        no_data = bool(_EMPTY_IMG_RE.search(img))
        obj = _graphic_named(graphics, name)
        if no_data and obj is None:
            missing.append(name)
            return match.group(0)
        marker = "wa%simg%d" % (nonce, len(images))
        if obj is not None:
            pic_obj: Any = obj
            kept = _read_picture(pic_obj, marker, name)
            in_region = (
                kept.anchor.value != "AT_PAGE"
                and uno_read(lambda: uno_same(pic_obj.getAnchor().getText(), text_obj), False) is True
            )
        else:
            kept = None
            in_region = False
        images.append(_RegionImage(marker, name, kept, no_data, in_region))
        if no_data:
            # StarWriter creates no graphic for <img src="">, so supply a 1x1 PNG stand-in
            # for the import and swap the original graphic back in restore_region_images.
            img = re.sub(
                r'\bsrc\s*=\s*(["\'])\s*\1',
                f'src="{_1PX_PNG_DATA_URI}"',
                img,
                count=1,
                flags=re.IGNORECASE,
            )
        # StarWriter's HTML import (HtmlOptionId::NAME -> SetFormatName in
        # sw/source/filter/html/htmlgrin.cxx) names the imported graphic from name=,
        # and border="0" stops a hyperlinked image from gaining 53/100 mm padding.
        return '<img name="%s" border="0"' % marker + img[4:]

    swapped = _FRAME_IMAGE_RE.sub(_swap, html)
    if missing or _EMPTY_IMG_RE.search(swapped):
        _refuse_lost_images(model, missing)
    linked: set[str] = set()
    for m in _LINKED_REGION_IMGS_RE.finditer(swapped):
        linked.update(_MARKER_RE.findall(m.group(0)))
    return swapped, [img._replace(linked=img.marker in linked) for img in images]


def restore_region_images(model: Any, images: list[_RegionImage]) -> None:
    """Restore each region image's anchor, placement, name, and graphic after HTML import."""
    if not images:
        return
    graphics = uno_read(lambda: model.getGraphicObjects(), None)
    for image in images:
        if graphics is None or not uno_read(lambda: graphics.hasByName(image.marker), False):
            raise ToolExecutionError(f"Image {image.name!r} was lost during header/footer import.")
        imported = graphics.getByName(image.marker)
        kept = image.kept
        if kept is None or kept.anchor.value == "AT_PAGE":
            if image.no_data and kept is not None:
                imported.Graphic = kept.graphic
            try:
                imported.setName(image.name)
            except Exception as exc:
                reraise_if_disposed(exc)
            continue
        # Only a graphic anchored directly in text_obj can survive the clear; a cell
        # original's _deleted_by_the_edit answers False even though Accept removes the table.
        if image.in_region and not _deleted_by_the_edit(model, kept.original, image.name):
            if image.linked or kept.props.get("HyperLinkURL"):
                _set_link(kept.original, _merged_link(imported, kept.props))
            imported.dispose()
            continue
        anchor = imported.getAnchor()
        anchor_text = anchor.getText()
        cursor = anchor_text.createTextCursorByRange(anchor.getStart())
        size = imported.getSize()
        props = dict(kept.props)
        try:
            props["Title"] = imported.getPropertyValue("Title")
        except Exception as exc:
            reraise_if_disposed(exc)
        if image.linked or props.get("HyperLinkURL"):
            props.update(_merged_link(imported, kept.props))
        graphic = kept.graphic if image.no_data else imported.getPropertyValue("Graphic")
        imported.dispose()
        # Insert a new TextGraphicObject instead of setting AnchorType on the imported one:
        # re-anchoring an imported graphic leaves two logos on a single Undo.
        _insert_copy(
            model,
            anchor_text,
            cursor,
            name=image.name,
            graphic=graphic,
            anchor=kept.anchor,
            size=size,
            props=props,
            absorb=False,
        )


def _deleted_by_the_edit(model: Any, original: Any, name: str) -> bool:
    """True when the edit removed *original*: missing from getGraphicObjects() or disposed
    (review off), or Accept All would remove it under a pending tracked deletion (review on).
    Anything else stays in place and must not be duplicated, e.g. a search replace of the text
    of the paragraph a picture is anchored to.
    """
    # hasByName is tested before getAnchor() because a deleted graphic can remain in
    # getGraphicObjects with a null anchor that crashes SwXFrame::getSwAnchor.
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
    if not is_recording_changes(model):
        return False
    if TrackedDeletions(model).removes_anchored(original):
        return True
    if original.getPropertyValue("AnchorType").value != "AT_PARAGRAPH":
        return False
    # A table the edit deletes whole takes its cells' pictures; no deletion covers the cell text.
    table = uno_read(
        lambda: anchor.getText().createTextCursorByRange(anchor.getStart()).getPropertyValue("TextTable"),
        None,
    )
    return table is not None and table_is_all_deleted(model, table)


def _restore_image_placeholders(model: Any, kept: list[_KeptImage]) -> None:
    """Put a copy of each kept picture where its marker landed, and remove the marker."""
    search = model.createSearchDescriptor()
    search.SearchCaseSensitive = True

    # Resolve every marker first so a missing marker raises with nothing half-restored;
    # restore only on the normal path (the failure path deliberately does not insert pictures,
    # because callers run under an atomic undo).
    plans: list[tuple[_KeptImage, Any, bool]] = []
    for image in kept:
        search.SearchString = image.marker
        found = model.findFirst(search)
        insert = (
            image.anchor.value != "AT_PAGE"
            and _deleted_by_the_edit(model, image.original, image.name)
        )
        if found is None:
            if insert:
                raise ToolExecutionError(f"Kept image {image.name!r} marker not found after import")
            log.warning("kept image %r: marker not found after the import", image.name)
            continue
        plans.append((image, found, insert))

    for image, found, insert in plans:
        if not insert:
            # The original is still where it was (page-anchored, or outside what the edit
            # replaced): apply any link change and drop the marker instead of adding a duplicate.
            if image.linked or image.props.get("HyperLinkURL"):
                _set_link(image.original, _merged_link(found, image.props))
            found.setString("")
            continue
        # Known LibreOffice limit (checked live, recorded or not): with review on, a picture in a
        # cell of a table this edit inserts keeps "Reject all" from removing that table. Accept is
        # right; Reject leaves the new table behind. Without the picture it would be lost instead.
        props = dict(image.props)
        # An unlinked picture inside a text link also has the link on its marker, so without
        # image.linked the marker's URL says nothing about the picture.
        if image.linked or props.get("HyperLinkURL"):
            props.update(_merged_link(found, props))
            if image.linked:
                # Otherwise the anchor keeps a text link and the next export has <a> inside <a>.
                with record_changes(model, False):
                    found.setPropertyValue("HyperLinkURL", "")
        _insert_copy(
            model,
            found.getText(),
            found,
            name=image.name,
            graphic=image.graphic,
            anchor=image.anchor,
            size=image.size,
            props=props,
            absorb=True,  # True: the marker text is replaced
        )


@contextlib.contextmanager
def keep_placeholder_images(
    model: Any,
    content: str,
    *,
    pre_swapped: tuple[str, list[_KeptImage]] | None = None,
) -> Generator[str, None, None]:
    """Yield *content* with data-less images swapped for markers; put the pictures back after.

    Callers that delete content before entering this context (such as
    ``_edit_full_document`` clearing leading tables) pass *pre_swapped* from
    ``capture_placeholder_images`` so pictures are read before anything is deleted.
    """
    swapped, kept = pre_swapped if pre_swapped is not None else capture_placeholder_images(model, content)
    yield swapped
    if kept:
        _restore_image_placeholders(model, kept)

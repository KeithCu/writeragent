# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Locate Writer notebook form control models (draw page + text enumeration fallback)."""

from __future__ import annotations

import logging
from typing import Any, Iterator

log = logging.getLogger("writeragent.notebook")

_CONTROL_SHAPE_TYPE = "com.sun.star.drawing.ControlShape"

__all__ = ["index_form_control_models", "find_form_control_model_by_name", "find_control_shape_by_name", "read_code_from_field"]


def _unwrap_form_model(obj: Any) -> Any | None:
    if obj is None:
        return None
    if hasattr(obj, "Text"):
        return obj
    control = getattr(obj, "Control", None)
    if control is not None:
        return control
    if hasattr(obj, "Name"):
        return obj
    return None


def _model_from_text_portion(portion: Any) -> Any | None:
    try:
        ptype = portion.getPropertyValue("TextPortionType")
    except Exception:
        ptype = getattr(portion, "TextPortionType", None)
    if ptype != "Frame":
        return None
    for attr in ("TextField", "TextContent", "TextEmbeddedObject"):
        try:
            embedded = getattr(portion, attr, None)
        except Exception:
            embedded = None
        model = _unwrap_form_model(embedded)
        if model is not None:
            return model
    return None


def _iter_control_shapes(doc: Any) -> Iterator[tuple[str, Any, Any]]:
    """Yield ``(name, model, shape)`` for in-flow ``ControlShape`` objects on the draw page."""
    if not hasattr(doc, "getDrawPage"):
        return
    try:
        dp = doc.getDrawPage()
        count = dp.getCount()
    except Exception:
        log.debug("notebook form lookup: draw page unavailable", exc_info=True)
        return
    for i in range(count):
        try:
            shape = dp.getByIndex(i)
            if shape.getShapeType() != _CONTROL_SHAPE_TYPE:
                continue
            model = shape.Control
        except Exception:
            continue
        if model is None:
            continue
        try:
            name = str(getattr(model, "Name", "") or "")
        except Exception:
            name = ""
        if name:
            yield name, model, shape


def _collect_models_from_draw_page(doc: Any, by_name: dict[str, Any]) -> None:
    """Writer registers in-flow ``ControlShape`` objects on the document draw page."""
    for name, model, _shape in _iter_control_shapes(doc):
        if name not in by_name:
            by_name[name] = model


def _collect_models_from_text(doc: Any, by_name: dict[str, Any]) -> None:
    """Fallback for tests and LO builds that expose controls only via text portions."""
    try:
        text = doc.getText()
        enum = text.createEnumeration()
    except Exception:
        return
    while enum.hasMoreElements():
        block = enum.nextElement()
        try:
            portion_enum = block.createEnumeration()
        except Exception:
            continue
        while portion_enum.hasMoreElements():
            portion = portion_enum.nextElement()
            model = _model_from_text_portion(portion)
            if model is None:
                continue
            try:
                name = getattr(model, "Name", "") or ""
            except Exception:
                name = ""
            if name and name not in by_name:
                by_name[name] = model


def index_form_control_models(doc: Any) -> dict[str, Any]:
    """Map form control ``Name`` → model for all notebook fields and ▶ buttons."""
    by_name: dict[str, Any] = {}
    _collect_models_from_draw_page(doc, by_name)
    if not by_name:
        _collect_models_from_text(doc, by_name)
    return by_name


def find_form_control_model_by_name(doc: Any, control_name: str) -> Any | None:
    if not control_name:
        return None
    return index_form_control_models(doc).get(control_name)


def find_control_shape_by_name(doc: Any, control_name: str) -> Any | None:
    """Return the draw-page ``ControlShape`` with ``Name == control_name``, or None.

    Used to reach ``shape.getAnchor()`` (paragraph of an in-flow field). Text-portion
    fallback in ``index_form_control_models`` has no shape, so this is draw-page only.
    """
    if not control_name:
        return None
    for name, _model, shape in _iter_control_shapes(doc):
        if name == control_name:
            return shape
    return None


def read_code_from_field(doc: Any, field_name: str) -> str | None:
    """Read multiline source from an in-flow form ``TextField`` by control name.

    ``None`` means the control is gone. ``""`` means the field is there and
    empty. Callers used to treat both as ``""``, so Run All skipped a deleted
    field with the same silence as a blank cell.
    """
    model = find_form_control_model_by_name(doc, field_name)
    if model is None or not hasattr(model, "Text"):
        return None
    return str(model.Text or "")

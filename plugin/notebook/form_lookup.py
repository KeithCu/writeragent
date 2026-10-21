# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Locate Writer notebook form control models (draw page + text enumeration fallback)."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import threading
from typing import Any, Iterator

from plugin.framework.errors import best_effort

_CONTROL_SHAPE_TYPE = "com.sun.star.drawing.ControlShape"

__all__ = [
    "ControlIndex",
    "build_control_index",
    "control_lookup_scope",
    "index_form_control_models",
    "find_form_control_model_by_name",
    "find_control_shape_by_name",
    "read_code_from_field",
]


@dataclass
class ControlIndex:
    doc: Any
    models_by_name: dict[str, Any]
    shapes_by_name: dict[str, Any]


_local = threading.local()


def _current_control_index() -> ControlIndex | None:
    # crosshair: off  # threading.local() is engine-hostile
    return getattr(_local, "index", None)


@contextlib.contextmanager
def control_lookup_scope(doc: Any) -> Iterator[None]:
    """Hold a draw-page control index for *doc* on this thread across nested calls."""
    # crosshair: off  # threading.local() is engine-hostile
    if doc is None:
        yield
        return
    prev = _current_control_index()
    if prev is not None and prev.doc is doc:
        yield
        return
    idx = build_control_index(doc)
    _local.index = idx
    try:
        yield
    finally:
        _local.index = prev


def build_control_index(doc: Any) -> ControlIndex:
    """Snapshot draw-page form control models and shapes once for *doc*."""
    models: dict[str, Any] = {}
    shapes: dict[str, Any] = {}
    for name, model, shape in _iter_control_shapes(doc):
        if name not in models:
            models[name] = model
        if name not in shapes:
            shapes[name] = shape
    if not models:
        _collect_models_from_text(doc, models)
    return ControlIndex(doc=doc, models_by_name=models, shapes_by_name=shapes)


def _index_for(doc: Any) -> ControlIndex:
    idx = _current_control_index()
    if idx is not None and idx.doc is doc:
        return idx
    return build_control_index(doc)


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
    ptype = portion.getPropertyValue("TextPortionType")
    if ptype != "Frame":
        return None
    for attr in ("TextField", "TextContent", "TextEmbeddedObject"):
        embedded = getattr(portion, attr, None)
        model = _unwrap_form_model(embedded)
        if model is not None:
            return model
    return None


def _iter_control_shapes(doc: Any) -> Iterator[tuple[str, Any, Any]]:
    """Yield ``(name, model, shape)`` for in-flow ``ControlShape`` objects on the draw page."""
    if not hasattr(doc, "getDrawPage"):
        return
    dp = doc.getDrawPage()
    count = dp.getCount()
    for i in range(count):
        shape = dp.getByIndex(i)
        if shape.getShapeType() != _CONTROL_SHAPE_TYPE:
            continue
        model = shape.Control
        if model is None:
            continue
        name = str(getattr(model, "Name", "") or "")
        if name:
            yield name, model, shape


def _collect_models_from_text(doc: Any, by_name: dict[str, Any]) -> None:
    """Fallback for tests and LO builds that expose controls only via text portions."""
    text = doc.getText()
    enum = text.createEnumeration()
    while enum.hasMoreElements() is True:  # a MagicMock return never stops a plain truth test
        block = enum.nextElement()
        # TextTable blocks have no portion enumeration. Skip the block.
        portion_enum = best_effort(
            lambda: block.createEnumeration(),
            "notebook form lookup: text block has no portion enumeration",
        )
        if portion_enum is None:
            continue
        while portion_enum.hasMoreElements() is True:
            portion = portion_enum.nextElement()
            model = _model_from_text_portion(portion)
            if model is None:
                continue
            name = str(getattr(model, "Name", "") or "")
            if name and name not in by_name:
                by_name[name] = model


def index_form_control_models(doc: Any) -> dict[str, Any]:
    """Map form control ``Name`` → model for all notebook fields and ▶ buttons."""
    return _index_for(doc).models_by_name


def find_form_control_model_by_name(doc: Any, control_name: str) -> Any | None:
    if not control_name:
        return None
    return _index_for(doc).models_by_name.get(control_name)


def find_control_shape_by_name(doc: Any, control_name: str) -> Any | None:
    """Return the draw-page ``ControlShape`` with ``Name == control_name``, or None.

    Used to reach ``shape.getAnchor()`` (paragraph of an in-flow field). Text-portion
    fallback in ``index_form_control_models`` has no shape, so this is draw-page only.
    """
    if not control_name:
        return None
    idx = _current_control_index()
    if idx is not None and idx.doc is doc:
        return idx.shapes_by_name.get(control_name)
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

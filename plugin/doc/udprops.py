# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""User-defined document property helpers (no Calc / document_helpers import graph).

Grammar persistence and other light callers need get/set udprops without pulling
``plugin.calc.bridge`` / ``analyzer`` via ``document_helpers``.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import uno

from plugin.framework import thread_guard
from plugin.framework.errors import (
    DocumentDisposedError,
    UnoObjectError,
    reraise_if_disposed,
    uno_call,
)

log = logging.getLogger(__name__)

# GHA 33763078357: hang dump named uno_call at getDocumentProperties. When
# True, each UNO step is also printed to stderr so the last line names the
# exact call. _clear_writeragent_udprops turns this on for insert_cell_html
# teardown only (production saves stay quiet).
_TRACE_UDPROPS = False


def _udprops_step(msg: str) -> None:
    """log.debug always; stderr only when teardown tracing is on."""
    log.debug(msg)
    if _TRACE_UDPROPS:
        print(msg, file=sys.stderr, flush=True)


def _user_defined_property_exists(props: Any, name: str) -> bool:
    """True iff ``name`` is on the PropertyBag (``XPropertySet``, not ``XNameAccess``)."""
    try:
        return bool(props.getPropertySetInfo().hasPropertyByName(name))
    except Exception as exc:
        log.debug("hasPropertyByName failed for %s (%s)", name, type(exc).__name__)
        return False


def _user_defined_properties(model: Any) -> Any:
    """Return the unwrapped UserDefinedProperties bag, or None when absent.

    PropertyBag hasPropertyByName / addProperty is unreliable through the
    guard proxy; unwrap the model then talk to the raw PropertyBag.
    """
    raw_model = thread_guard.unwrap_uno(model)
    if raw_model is None:
        raise UnoObjectError("Document Model is null", code="UNO_NULL_OBJECT")
    if not hasattr(raw_model, "getDocumentProperties"):
        return None
    doc_props = _document_properties_bag(raw_model)
    if doc_props is None:
        return None
    props = thread_guard.unwrap_uno(getattr(doc_props, "UserDefinedProperties", None))
    return props


def get_document_property(model: Any, name: str, default: Any = None) -> Any:
    """Get a custom document property from the model.

    Called from XFilter.filter() on LO's dispatch thread (Dummy-* when the
    client is a socket bootstrap), so this is not @main_thread_only.
    """
    try:
        props = _user_defined_properties(model)
        if props is None:
            return default
        if _user_defined_property_exists(props, name):
            return uno_call(lambda: props.getPropertyValue(name), "Get property value")
        return default
    except UnoObjectError:
        if name in ("WriterAgentSessionID", "WriterAgentPythonSessionId"):
            log.debug("get_document_property (optional property %s not set yet)", name)
        else:
            log.exception("get_document_property failed")
    except DocumentDisposedError:
        raise
    except Exception:
        log.exception("Unexpected error in get_document_property")
    return default


def set_document_property(model: Any, name: str, value: Any) -> bool:
    """Set a custom document property in the model.

    Same dispatch-thread note as get_document_property (notebook File Open).
    """
    try:
        _udprops_step("set_document_property: getDocumentProperties start name=%s" % name)
        props = _user_defined_properties(model)
        _udprops_step("set_document_property: getDocumentProperties done")
        if props is None:
            return False

        if _user_defined_property_exists(props, name):
            if not hasattr(props, "setPropertyValue"):
                return False
            _udprops_step("set_document_property: setPropertyValue start name=%s" % name)
            uno_call(lambda: props.setPropertyValue(name, str(value)), "Set property value")
            _udprops_step("set_document_property: setPropertyValue done")
        else:
            if not hasattr(props, "addProperty"):
                return False
            REMOVABLE = uno.getConstantByName("com.sun.star.beans.PropertyAttribute.REMOVABLE")
            _udprops_step("set_document_property: addProperty start name=%s" % name)
            uno_call(lambda: props.addProperty(name, REMOVABLE, str(value)), "Add property")
            _udprops_step("set_document_property: addProperty done")
        return True

    except UnoObjectError:
        doc_url = ""
        readonly = ""
        try:
            raw_model = thread_guard.unwrap_uno(model)
            if hasattr(raw_model, "getURL"):
                doc_url = raw_model.getURL() or ""
            if hasattr(raw_model, "isReadonly"):
                readonly = str(raw_model.isReadonly())
        except Exception as exc:
            log.debug("set_document_property url/readonly probe failed (%s)", type(exc).__name__)

        log.exception("set_document_property error (url=%s, readonly=%s)", doc_url, readonly)
        raise

def _document_properties_bag(model: Any) -> Any | None:
    """Return the unwrapped ``getDocumentProperties()`` object, or None."""
    raw_model = thread_guard.unwrap_uno(model)
    if raw_model is None:
        raise UnoObjectError("Document Model is null", code="UNO_NULL_OBJECT")
    if not hasattr(raw_model, "getDocumentProperties"):
        return None
    return uno_call(raw_model.getDocumentProperties, "Get document properties")


def list_user_defined_document_properties(model: Any) -> list[str]:
    """Sorted names on the document's UserDefinedProperties bag (may be empty)."""
    props = _user_defined_properties(model)
    if props is None:
        return []
    try:
        info = uno_call(props.getPropertySetInfo, "Get user-defined property set info")
        if info is None:
            return []
        names: list[str] = []
        for prop in uno_call(info.getProperties, "List user-defined properties"):
            name = getattr(prop, "Name", None)
            if name:
                names.append(str(name))
        return sorted(names)
    except DocumentDisposedError:
        raise
    except Exception:
        log.exception("list_user_defined_document_properties failed")
        return []


def _keywords_to_str_list(keywords: Any) -> list[str]:
    if keywords is None:
        return []
    if isinstance(keywords, (list, tuple)):
        return [str(k) for k in keywords]
    return [str(keywords)]


_BUILTIN_DOCUMENT_PROPERTY_FIELDS: tuple[tuple[str, str], ...] = (
    ("title", "Title"),
    ("subject", "Subject"),
    ("author", "Author"),
    ("description", "Description"),
)


def read_builtin_document_properties(model: Any) -> dict[str, Any]:
    """Built-in metadata on ``getDocumentProperties()`` (strings + keyword list)."""
    doc_props = _document_properties_bag(model)
    if doc_props is None:
        return {
            "title": "",
            "subject": "",
            "author": "",
            "description": "",
            "keywords": [],
        }
    out: dict[str, Any] = {}
    for field_key, attr in _BUILTIN_DOCUMENT_PROPERTY_FIELDS:

        def _read_field(a: str = attr, dp: Any = doc_props) -> str:
            return str(getattr(dp, a, None) or "")

        out[field_key] = uno_call(_read_field, f"Read {attr}")
    keywords_raw = uno_call(lambda: getattr(doc_props, "Keywords", None), "Read Keywords")
    out["keywords"] = _keywords_to_str_list(keywords_raw)
    return out


def write_builtin_document_properties(model: Any, **fields: Any) -> list[str]:
    """Write built-in metadata fields present in *fields*. Returns keys updated."""
    doc_props = _document_properties_bag(model)
    if doc_props is None:
        return []
    updated: list[str] = []
    for field_key, attr in _BUILTIN_DOCUMENT_PROPERTY_FIELDS:
        if field_key not in fields:
            continue
        val = fields[field_key]

        def _write_attr(a: str = attr, v: Any = val, dp: Any = doc_props) -> None:
            setattr(dp, a, str(v))

        uno_call(_write_attr, f"Write {attr}")
        updated.append(field_key)
    if "keywords" in fields:
        kw = fields["keywords"]

        def _write_keywords(k: Any = kw, dp: Any = doc_props) -> None:
            setattr(dp, "Keywords", tuple(str(item) for item in k))

        uno_call(_write_keywords, "Write Keywords")
        updated.append("keywords")
    return updated


def remove_document_property(doc: Any, name: str) -> bool:
    """Remove a custom document property. True when it is absent afterwards."""
    try:
        props = _user_defined_properties(doc)
        if props is None or not _user_defined_property_exists(props, name):
            return True
        uno_call(lambda: props.removeProperty(name), "Remove property")
        return not _user_defined_property_exists(props, name)
    except Exception as exc:
        reraise_if_disposed(exc)
        log.debug("remove_document_property %s failed (%s)", name, type(exc).__name__)
        return False

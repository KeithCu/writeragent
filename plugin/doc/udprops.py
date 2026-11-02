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
    raw_model = thread_guard._unwrap_uno(model)
    if raw_model is None:
        raise UnoObjectError("Document Model is null", code="UNO_NULL_OBJECT")
    if not hasattr(raw_model, "getDocumentProperties"):
        return None
    doc_props = uno_call(raw_model.getDocumentProperties, "Get document properties")
    if doc_props is None:
        return None
    props = thread_guard._unwrap_uno(getattr(doc_props, "UserDefinedProperties", None))
    if props is not None:
        if props is None:
            raise UnoObjectError("UserDefinedProperties is null", code="UNO_NULL_OBJECT")
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
            raw_model = thread_guard._unwrap_uno(model)
            if hasattr(raw_model, "getURL"):
                doc_url = raw_model.getURL() or ""
            if hasattr(raw_model, "isReadonly"):
                readonly = str(raw_model.isReadonly())
        except Exception as exc:
            log.debug("set_document_property url/readonly probe failed (%s)", type(exc).__name__)

        log.exception("set_document_property error (url=%s, readonly=%s)", doc_url, readonly)
        raise

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

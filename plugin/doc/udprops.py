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

from plugin.framework.errors import (
    DocumentDisposedError,
    UnoObjectError,
    check_disposed,
    reraise_if_disposed,
    safe_call,
)

log = logging.getLogger(__name__)

# GHA 33763078357: hang dump named safe_call at getDocumentProperties. When
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
    """Return True iff ``name`` is already defined on ``UserDefinedProperties``.

    ``UserDefinedProperties`` is a ``com.sun.star.beans.PropertyBag`` which
    implements ``XPropertySet`` (offering ``getPropertySetInfo().hasPropertyByName``)
    and ``XPropertyContainer`` (``addProperty``/``removeProperty``) — but not
    ``XNameAccess``. So ``hasattr(props, "hasByName")`` is False, the old
    ``not exists`` branch always fired, and the second save raised
    ``Property name or handle already used``.
    """
    if hasattr(props, "getPropertySetInfo"):
        try:
            info = props.getPropertySetInfo()
        except Exception:
            info = None
        if info is not None and hasattr(info, "hasPropertyByName"):
            try:
                return bool(info.hasPropertyByName(name))
            except Exception as exc:
                log.debug("hasPropertyByName failed for %s (%s)", name, type(exc).__name__)
    if hasattr(props, "hasByName"):
        try:
            return bool(props.hasByName(name))
        except Exception as exc:
            log.debug("hasByName failed for %s (%s)", name, type(exc).__name__)
    return False


def _user_defined_properties(raw_model: Any) -> Any:
    """Return the unwrapped UserDefinedProperties bag, or None when absent."""
    from plugin.framework.thread_guard import _unwrap_uno

    if not hasattr(raw_model, "getDocumentProperties"):
        return None
    doc_props = safe_call(raw_model.getDocumentProperties, "Get document properties")
    if doc_props is None:
        return None
    props = _unwrap_uno(getattr(doc_props, "UserDefinedProperties", None))
    if props is not None:
        check_disposed(props, "UserDefinedProperties")
    return props


def get_document_property(model: Any, name: str, default: Any = None) -> Any:
    """Get a custom document property from the model.

    Called from XFilter.filter() on LO's dispatch thread (Dummy-* when the
    client is a socket bootstrap), so this is not @main_thread_only.
    """
    try:
        from plugin.framework.thread_guard import _unwrap_uno

        # PropertyBag hasPropertyByName / addProperty is unreliable through the
        # guard proxy; unwrap the model then talk to the raw PropertyBag.
        raw_model = _unwrap_uno(model)
        check_disposed(raw_model, "Document Model")
        props = _user_defined_properties(raw_model)
        if props is None:
            return default
        if _user_defined_property_exists(props, name):
            return safe_call(props.getPropertyValue, "Get property value", name)
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
    raw_model = model
    try:
        from plugin.framework.thread_guard import _unwrap_uno

        # Same PropertyBag unwrap as get_document_property.
        raw_model = _unwrap_uno(model)
        check_disposed(raw_model, "Document Model")
        _udprops_step("set_document_property: getDocumentProperties start name=%s" % name)
        props = _user_defined_properties(raw_model)
        _udprops_step("set_document_property: getDocumentProperties done")
        if props is None:
            return False

        if _user_defined_property_exists(props, name):
            if not hasattr(props, "setPropertyValue"):
                return False
            _udprops_step("set_document_property: setPropertyValue start name=%s" % name)
            safe_call(props.setPropertyValue, "Set property value", name, str(value))
            _udprops_step("set_document_property: setPropertyValue done")
        else:
            if not hasattr(props, "addProperty"):
                return False
            REMOVABLE = uno.getConstantByName("com.sun.star.beans.PropertyAttribute.REMOVABLE")
            _udprops_step("set_document_property: addProperty start name=%s" % name)
            safe_call(props.addProperty, "Add property", name, REMOVABLE, str(value))
            _udprops_step("set_document_property: addProperty done")
        return True

    except UnoObjectError:
        doc_url = ""
        readonly = ""
        try:
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
    from plugin.framework.thread_guard import _unwrap_uno

    try:
        raw_doc = _unwrap_uno(doc)
        check_disposed(raw_doc, "Document Model")
        props = _user_defined_properties(raw_doc)
        if props is None or not _user_defined_property_exists(props, name):
            return True
        safe_call(props.removeProperty, "Remove property", name)
        return not _user_defined_property_exists(props, name)
    except Exception as exc:
        reraise_if_disposed(exc)
        log.debug("remove_document_property %s failed (%s)", name, type(exc).__name__)
        return False

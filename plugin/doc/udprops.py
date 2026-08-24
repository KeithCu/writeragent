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
from typing import Any

import uno

from plugin.framework.errors import UnoObjectError, check_disposed, safe_call

log = logging.getLogger(__name__)


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
            except Exception:
                pass
    if hasattr(props, "hasByName"):
        try:
            return bool(props.hasByName(name))
        except Exception:
            pass
    return False


def get_document_property(model: Any, name: str, default: Any = None) -> Any:
    """Get a custom document property from the model."""
    try:
        from plugin.framework.thread_guard import _unwrap_uno

        raw_model = _unwrap_uno(model)
        check_disposed(raw_model, "Document Model")
        if hasattr(raw_model, "getDocumentProperties"):
            doc_props = safe_call(raw_model.getDocumentProperties, "Get document properties")
            if doc_props is None:
                return default
            props = _unwrap_uno(getattr(doc_props, "UserDefinedProperties", None))
            if props is None:
                return default

            check_disposed(props, "UserDefinedProperties")

            if _user_defined_property_exists(props, name):
                return safe_call(props.getPropertyValue, "Get property value", name)
            return default
    except UnoObjectError:
        if name in ("WriterAgentSessionID", "WriterAgentPythonSessionId"):
            log.debug("get_document_property (optional property %s not set yet)", name)
        else:
            log.exception("get_document_property failed")
    except Exception:
        log.exception("Unexpected error in get_document_property")
    return default


def set_document_property(model: Any, name: str, value: Any) -> None:
    """Set a custom document property in the model."""
    try:
        from plugin.framework.thread_guard import _unwrap_uno

        raw_model = _unwrap_uno(model)
        check_disposed(raw_model, "Document Model")
        if hasattr(raw_model, "getDocumentProperties"):
            doc_props = safe_call(raw_model.getDocumentProperties, "Get document properties")
            if doc_props is None:
                return
            props = _unwrap_uno(getattr(doc_props, "UserDefinedProperties", None))
            if props is not None:
                check_disposed(props, "UserDefinedProperties")

                exists = _user_defined_property_exists(props, name)

                if exists and hasattr(props, "setPropertyValue"):
                    safe_call(props.setPropertyValue, "Set property value", name, str(value))
                elif hasattr(props, "addProperty"):
                    REMOVABLE = uno.getConstantByName("com.sun.star.beans.PropertyAttribute.REMOVABLE")
                    safe_call(props.addProperty, "Add property", name, REMOVABLE, str(value))
                elif hasattr(props, "setPropertyValue"):
                    safe_call(props.setPropertyValue, "Set property value (no addProperty)", name, str(value))

    except UnoObjectError:
        doc_url = ""
        readonly = ""
        try:
            if hasattr(model, "getURL"):
                doc_url = model.getURL() or ""
            if hasattr(model, "isReadonly"):
                readonly = str(model.isReadonly())
        except Exception:
            pass

        log.exception("set_document_property error (url=%s, readonly=%s)", doc_url, readonly)
        raise

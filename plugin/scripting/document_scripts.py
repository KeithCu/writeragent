# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Document-attached named Python scripts and Calc workbook init scripts (UserDefinedProperties).

Venv code can import library defs via ``wa.scripts.<Name>`` (My Scripts) and
``wa.doc.<Name>`` (This Document). See ``plugin.scripting.named_scripts``.
``run_venv_python_script`` stays blocked (would re-enter the warm worker).
"""

# =========================================================================================
# WARNING: PARITY INVARIANT WITH MONACO JAVASCRIPT FRONTEND
# If you modify script sections, envelope structures, or script list IPC payloads here,
# you MUST also update the corresponding JavaScript / Python consumers:
#   - JS Script Manager:        plugin/contrib/scripting/assets/editor/scripts_manager.js
#   - Python UI Dialog:         plugin/scripting/python_runner_ui.py
#   - IPC Host Bridge:          plugin/scripting/editor_host.py
# =========================================================================================

from __future__ import annotations

from enum import Enum
import hashlib
import json
import logging
from typing import Any, Callable

from plugin.doc.doc_type import is_calc, is_draw, is_writer
from plugin.doc.udprops import get_document_property, set_document_property
from plugin.framework import config
from plugin.framework.errors import DocumentDisposedError
from plugin.framework.i18n import _
from plugin.framework.uno_context import _enumeration_should_continue, get_desktop, normalize_doc_url
from plugin.scripting.domain_registry import (
    DOC_SCRIPT_DISPLAY_PREFIX,
    SCRIPT_ORIGIN_DOCUMENT,
    SCRIPT_ORIGIN_USER,
    domain_template_entries,
    get_picker_domains,
    is_reserved_script_name,
    parse_picker_display_name,
    picker_display_name,
)
from plugin.scripting.session_manager import calc_init_session_id

log = logging.getLogger(__name__)

DOCUMENT_SCRIPTS_UDPROP = "WriterAgentDocumentPythonScripts"
_MAX_DOCUMENT_SCRIPTS_BYTES = 900_000
_SOFT_WARN_SCRIPT_BYTES = 200_000
_ENVELOPE_VERSION = 1
# Workbook init lives in the same map as named document scripts. The picker and
# wa.doc must not expose it; deleting "[Doc] INIT" used to wipe the init script.
_CALC_INIT_SCRIPT_NAMES = frozenset({"INIT", "Init"})


class DocumentScriptErrorCode(str, Enum):
    """Distinguishable error codes for document and user script persistence."""

    NO_DOCUMENT = "no_document"
    TOO_LARGE = "too_large"
    READONLY = "readonly"
    RESERVED_NAME = "reserved_name"
    ALREADY_EXISTS = "already_exists"
    EMPTY_NAME = "empty_name"
    NOT_FOUND = "not_found"
    UNREADABLE = "unreadable"


class DocumentScriptError(str):
    """String subclass carrying a machine-readable error code."""

    code: DocumentScriptErrorCode

    def __new__(cls, message: str, code: DocumentScriptErrorCode) -> DocumentScriptError:
        obj = super().__new__(cls, message)
        obj.code = code
        return obj


def document_scripts_identity(doc: Any) -> str:
    """Stable identity for stale detection (normalized URL or empty for untitled)."""
    try:
        if hasattr(doc, "getURL"):
            return normalize_doc_url(doc.getURL() or "")
    except Exception:
        log.debug("document_scripts_identity failed", exc_info=True)
    return ""


def is_document_readonly_for_scripts(doc: Any) -> bool:
    if doc is None:
        return True
    try:
        if hasattr(doc, "isReadonly") and doc.isReadonly():
            return True
    except Exception:
        log.debug("document_scripts: isReadonly check failed", exc_info=True)
    return False


def _envelope_to_json(scripts: dict[str, str]) -> str:
    return json.dumps(
        {"version": _ENVELOPE_VERSION, "scripts": scripts},
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _unreadable_document_scripts() -> DocumentScriptError:
    return DocumentScriptError(
        _("Stored document scripts are unreadable, so they were left unchanged."),
        DocumentScriptErrorCode.UNREADABLE,
    )


def _fail_unreadable(message: str, *args: object, exc_info: bool = False) -> tuple[dict[str, str], DocumentScriptError]:
    log.warning(message, *args, exc_info=exc_info)
    return {}, _unreadable_document_scripts()


def _unsupported_document_scripts(version: object) -> DocumentScriptError:
    return DocumentScriptError(
        _("Stored document scripts are in an unsupported format (version {0}), so they were left unchanged.").format(
            version
        ),
        DocumentScriptErrorCode.UNREADABLE,
    )


def _readonly_document_error() -> DocumentScriptError:
    """Document is read-only or properties cannot be written.

    This sentence does not claim a My Scripts save; only the read-only
    fallback does, and only after this error returns.
    """
    return DocumentScriptError(
        _("Document is read-only or properties cannot be written."),
        DocumentScriptErrorCode.READONLY,
    )


def _load_document_script_map(doc: Any) -> tuple[dict[str, str], DocumentScriptError | None]:
    if doc is None:
        return {}, None
    raw = get_document_property(doc, DOCUMENT_SCRIPTS_UDPROP, default=None)
    if raw is None:
        return {}, None
    text = str(raw).strip()
    if not text:
        return {}, None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return _fail_unreadable("document_scripts: failed to parse JSON envelope", exc_info=True)
    if not isinstance(parsed, dict):
        return _fail_unreadable("document_scripts: expected object, got %s", type(parsed).__name__)
    if "version" not in parsed:
        return _fail_unreadable("document_scripts: missing version")
    if parsed.get("version") != _ENVELOPE_VERSION:
        log.warning("document_scripts: unsupported version %r", parsed.get("version"))
        return {}, _unsupported_document_scripts(parsed.get("version"))
    scripts_raw = parsed.get("scripts")
    if not isinstance(scripts_raw, dict):
        return _fail_unreadable("document_scripts: missing scripts map")
    out: dict[str, str] = {}
    for key, value in scripts_raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return _fail_unreadable("document_scripts: non-string script entry")
        out[key] = value
    return out, None


def get_document_scripts(doc: Any) -> dict[str, str]:
    scripts, _err = _load_document_script_map(doc)
    return scripts


def _encoded_envelope(scripts: dict[str, str]) -> tuple[str, DocumentScriptError | None]:
    payload = _envelope_to_json(scripts)
    nbytes = len(payload.encode("utf-8"))
    if nbytes > _MAX_DOCUMENT_SCRIPTS_BYTES:
        return payload, DocumentScriptError(
            _("Document scripts are too large to store in the document ({0} bytes).").format(nbytes),
            DocumentScriptErrorCode.TOO_LARGE,
        )
    for name, code in scripts.items():
        script_bytes = len((code or "").encode("utf-8"))
        if script_bytes > _SOFT_WARN_SCRIPT_BYTES:
            log.warning("document_scripts: script %r is large (%d bytes)", name, script_bytes)
    return payload, None


def set_document_scripts(doc: Any, scripts: dict[str, str]) -> DocumentScriptError | None:
    if doc is None:
        return DocumentScriptError(
            _("No document is open to save scripts."),
            DocumentScriptErrorCode.NO_DOCUMENT,
        )
    payload, err = _encoded_envelope(scripts)
    if err:
        return err
    if is_document_readonly_for_scripts(doc):
        # The previous sentence claimed a My Scripts write that only two
        # callers perform. Save As, New, and Monaco attach show this string
        # and do not write My Scripts.
        return _readonly_document_error()
    try:
        set_document_property(doc, DOCUMENT_SCRIPTS_UDPROP, payload)
    except Exception as exc:
        # A disposed document is not "read-only". Callers that swallow the
        # message used to keep writing as if the file were still open.
        from plugin.framework.errors import is_disposed_exception

        if is_disposed_exception(exc):
            raise
        log.exception("document_scripts: failed to persist on document")
        # Same false My Scripts claim as the read-only return above.
        return _readonly_document_error()
    # set_document_property returns None after a real write and also when the
    # document has no UserDefinedProperties bag (that path does not raise).
    # The missing bag used to look like success while the next read was empty.
    stored = get_document_property(doc, DOCUMENT_SCRIPTS_UDPROP, default=None)
    if stored != payload:
        log.error("document_scripts: persist did not store %s", DOCUMENT_SCRIPTS_UDPROP)
        return DocumentScriptError(
            _("Document properties cannot be written (missing or unwriteable UserDefinedProperties)."),
            DocumentScriptErrorCode.READONLY,
        )
    return None


def get_calc_init_script(doc: Any, *, default: str = "") -> str:
    """Return the workbook init script on *doc*, checking INIT first then Init."""
    scripts = get_document_scripts(doc)
    # ``scripts.get("INIT") or scripts.get("Init")`` treats an empty "INIT"
    # as missing and falls through to a stale "Init". Check key presence so
    # an explicit empty "INIT" is kept.
    if "INIT" in scripts:
        return scripts["INIT"]
    if "Init" in scripts:
        return scripts["Init"]
    return default


def set_calc_init_script(doc: Any, code: str) -> DocumentScriptError | None:
    """Persist init script on *doc* under the name 'INIT' in document scripts."""
    loaded, err = _load_document_script_map(doc)
    if err:
        return err
    scripts = dict(loaded)
    # If both 'INIT' and 'Init' exist, a write to 'Init' while reads prefer
    # 'INIT' makes saves look like no-ops. Drop both keys and store one 'INIT'.
    scripts.pop("INIT", None)
    scripts.pop("Init", None)
    if code:
        scripts["INIT"] = code
    # =PY() reads the init script from the calling document on each UI-thread
    # evaluation, so there is no host-side cache to refresh here.
    return set_document_scripts(doc, scripts)


def _enumerate_calc_documents(desktop: Any) -> list[Any]:
    """Calc models from desktop.getComponents(), in enumeration order."""
    matches: list[Any] = []
    try:
        comps = desktop.getComponents()
        if not comps:
            return matches
        enum = comps.createEnumeration()
        if not enum:
            return matches
        # _enumerate_calc_documents used to continue when nextElement() raised
        # while hasMoreElements() stayed true, so a stuck desktop enum spun
        # on the UI thread. There was no cap, and any truthy hasMoreElements()
        # counted as more. uno_context._enumeration_should_continue stops
        # at _ENUM_CAP and accepts only True or 1. Counting every attempt, then
        # continuing, stops the spin and still keeps a later Calc model when one
        # element's getURL throws. iter_desktop_models breaks on nextElement
        # and would drop that later model.
        seen = 0
        while True:
            if not _enumeration_should_continue(enum, seen, label="document_scripts"):
                break
            seen += 1
            try:
                elem = enum.nextElement()
            except Exception:
                log.debug("document_scripts: nextElement failed; trying later components", exc_info=True)
                continue
            try:
                model = None
                if hasattr(elem, "getURL") and callable(getattr(elem, "getURL")):
                    model = elem
                elif hasattr(elem, "getController") and elem.getController():
                    model = elem.getController().getModel()
                if model and is_calc(model):
                    matches.append(model)
            except Exception:
                # Catch per element so one disposed or broken element does not abort enumeration
                log.debug("document_scripts: skipping invalid component during enumeration", exc_info=True)
                continue
    except Exception:
        log.debug("document_scripts: Calc component enumeration failed", exc_info=True)
    return matches


def _select_enumerated_calc_document(matches: list[Any]) -> Any | None:
    """The only open Calc model, or None when there are zero or several.

    UNO component order is not the focused file, so with several workbooks
    open no enumerated match is safe to pick.
    """
    if len(matches) == 1:
        from plugin.framework.thread_guard import guard_uno

        return guard_uno(matches[0])
    return None


def get_calc_document_from_ctx(ctx: Any) -> Any | None:
    try:
        desktop = get_desktop(ctx)
        doc = desktop.getCurrentComponent()
    except Exception:
        log.debug("document_scripts: could not resolve active Calc document", exc_info=True)
        return None
    if doc is not None and (is_writer(doc) or is_draw(doc)):
        # What was wrong: comments said Impress still enumerates.
        # How: is_draw is Draw or Impress (plugin/doc/doc_type.py). 725df4915
        # returned None for Writer or is_draw so a focused non-Calc document
        # would not bind a background workbook. e9e5f4b35 described that check
        # as Writer and Draw only.
        # Why this stays: a focused Impress document must not bind a Calc init
        # script. Base and Math are not is_draw, so they still fall through
        # to _enumerate_calc_documents.
        return None
    if doc is None or not is_calc(doc):
        return _select_enumerated_calc_document(_enumerate_calc_documents(desktop))
    from plugin.framework.thread_guard import guard_uno

    return guard_uno(doc)


def init_script_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def build_python_eval_init_kwargs(doc: Any) -> dict[str, Any]:
    """Kwargs for ``run_code_in_user_venv`` init execution (pass with separate ``session_id=``)."""
    init_code = (get_calc_init_script(doc) or "").strip()
    if not init_code:
        return {}
    return {
        "init_script": init_code,
        "init_session_id": calc_init_session_id(doc),
        "init_script_hash": init_script_hash(init_code),
    }


def new_script_name_error(name: str, *, attach: bool) -> str | None:
    """Storage sentence for a stripped *name*, or None when that name is allowed.

    Does not strip. A whitespace-only string is not empty; callers strip.
    ``INIT`` and ``Init`` are the workbook init keys in the document property
    map, so they are rejected only when *attach* is true. The same spellings
    are legal My Scripts names. ``init`` is not in ``_CALC_INIT_SCRIPT_NAMES``.
    """
    if not name:
        return _("Script name cannot be empty.")
    if is_reserved_script_name(name):
        return _("'{0}' starts with a reserved prefix.").format(name)
    if attach and is_calc_init_script_name(name):
        return _("'{0}' is reserved for the workbook init script.").format(name)
    return None


def attach_document_script(
    doc: Any,
    name: str,
    code: str,
    *,
    overwrite: bool = False,
) -> DocumentScriptError | None:
    name = (name or "").strip()
    message = new_script_name_error(name, attach=True)
    if message is not None:
        # What was wrong: this branch assigned the script-body parameter code to an error-code enum.
        # How: 26eec1a72 folded the empty, prefix, and INIT checks into new_script_name_error and reused the name code.
        # Why this fixes it: the return is immediate, so nothing was stored, but a later edit that fell through would persist empty_name or reserved_name as the script. code stays the body.
        error_code = (
            DocumentScriptErrorCode.EMPTY_NAME
            if not name
            else DocumentScriptErrorCode.RESERVED_NAME
        )
        return DocumentScriptError(message, error_code)
    loaded, err = _load_document_script_map(doc)
    if err:
        return err
    scripts = dict(loaded)
    if name in scripts and not overwrite:
        return DocumentScriptError(
            _("A script named '{0}' already exists in this document.").format(name),
            DocumentScriptErrorCode.ALREADY_EXISTS,
        )
    scripts[name] = code if code is not None else ""
    return set_document_scripts(doc, scripts)


def delete_document_script(doc: Any, name: str) -> DocumentScriptError | None:
    # INIT shares this property map with named scripts. The picker hides it,
    # but deleting the storage name used to pop the entry and wipe the
    # workbook init script. attach_document_script already rejects this name.
    if is_calc_init_script_name(name):
        return DocumentScriptError(
            _("'{0}' is reserved for the workbook init script.").format(name),
            DocumentScriptErrorCode.RESERVED_NAME,
        )
    loaded, err = _load_document_script_map(doc)
    if err:
        return err
    scripts = dict(loaded)
    # Deleting a script that is not there must not report success or rewrite
    # document properties. Return NOT_FOUND and skip set_document_scripts.
    if name not in scripts:
        return DocumentScriptError(
            _("Script '{0}' does not exist in this document.").format(name),
            DocumentScriptErrorCode.NOT_FOUND,
        )
    del scripts[name]
    return set_document_scripts(doc, scripts)


def save_document_script(doc: Any, name: str, code: str) -> DocumentScriptError | None:
    return attach_document_script(doc, name, code, overwrite=True)


def document_script_display_name(name: str) -> str:
    return picker_display_name(DOC_SCRIPT_DISPLAY_PREFIX, name)


def parse_document_script_display_name(display: str) -> str | None:
    return parse_picker_display_name(DOC_SCRIPT_DISPLAY_PREFIX, display)


def is_calc_init_script_name(name: str) -> bool:
    """True for the workbook init entry stored beside named document scripts."""
    return name in _CALC_INIT_SCRIPT_NAMES


def script_origin_is_writable(origin: str) -> bool:
    """True for writable script origins ("", My Scripts, this-document).

    An unspecified origin ("") stays writable so default scripts take the My Scripts branch.
    Builtin origins (vision, math, and other picker domain origins) are read-only.
    """
    return origin in ("", SCRIPT_ORIGIN_USER, SCRIPT_ORIGIN_DOCUMENT)


def document_scripts_write_is_stale(session_doc: Any | None, session_doc_url: str | None) -> bool:
    """True when the document opened with the editor is no longer that same file.

    The picker used to save onto whichever window was focused. Writes stay on
    the launch document, and are refused when that document's identity changed.
    """
    if session_doc is None or session_doc_url is None:
        return False
    # An untitled file captures "". After File → Save the same component has
    # a URL. That is not a different document. Go stale only when a non-empty
    # captured URL no longer matches.
    if session_doc_url == "":
        return False
    return document_scripts_identity(session_doc) != session_doc_url


def picker_document_scripts(scripts: dict[str, str]) -> dict[str, str]:
    """Document scripts shown in the picker and ``wa.doc`` (init script omitted)."""
    return {name: code for name, code in scripts.items() if not is_calc_init_script_name(name)}


def resolve_script_picker_entry(display_name: str, origin_map: dict[str, str]) -> tuple[str, str]:
    """Return (real_name, origin) for a listbox/display label."""
    origin = origin_map.get(display_name, SCRIPT_ORIGIN_USER)
    if origin == SCRIPT_ORIGIN_DOCUMENT:
        real = parse_document_script_display_name(display_name)
        return (real or display_name, SCRIPT_ORIGIN_DOCUMENT)
    for domain in get_picker_domains():
        if origin == domain.origin:
            real = parse_picker_display_name(domain.display_prefix, display_name)
            return (real or display_name, domain.origin)
    return (display_name, SCRIPT_ORIGIN_USER)


def _picker_state_from_parts(
    user_scripts: dict[str, str],
    doc_scripts: dict[str, str],
    domain_entries: list[tuple[Any, dict[str, str]]],
) -> tuple[list[str], dict[str, str], dict[str, str]]:
    origin_map: dict[str, str] = {}
    merged: dict[str, str] = {}

    for name in sorted(user_scripts.keys()):
        origin_map[name] = SCRIPT_ORIGIN_USER
        merged[name] = user_scripts[name]

    for name in sorted(picker_document_scripts(doc_scripts)):
        display = document_script_display_name(name)
        origin_map[display] = SCRIPT_ORIGIN_DOCUMENT
        merged[display] = doc_scripts[name]

    domain_items: list[str] = []
    for domain, display_scripts in domain_entries:
        for display_name, code in display_scripts.items():
            origin_map[display_name] = domain.origin
            merged[display_name] = code
            domain_items.append(display_name)

    items = (
        sorted(user_scripts.keys())
        + [document_script_display_name(n) for n in sorted(picker_document_scripts(doc_scripts))]
        + domain_items
    )
    return items, merged, origin_map


def build_xdl_script_picker_state(
    doc: Any | None,
    saved_scripts: dict[str, str],
) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """Return (dropdown_items, merged_scripts_by_display_key, origin_map)."""
    user_scripts = dict(saved_scripts) if isinstance(saved_scripts, dict) else {}
    doc_scripts = get_document_scripts(doc) if doc is not None else {}
    domain_entries = domain_template_entries(doc)
    return _picker_state_from_parts(user_scripts, doc_scripts, domain_entries)


def resolve_run_script_name_config_key(doc: Any) -> str:
    """Return the config key for persisting the last selected Run Python Script name for *doc*."""
    if doc:
        if is_calc(doc):
            return "last_python_script_name_calc"
        if is_draw(doc):
            return "last_python_script_name_draw"
    return "last_python_script_name_writer"


def resolve_run_script_selection(
    doc: Any | None,
    picker_state: tuple[list[str], dict[str, str], dict[str, str]],
) -> tuple[str, str, dict[str, str]]:
    """Return (selected_name, selected_code, merged_scripts) for Run Python Script."""
    name_config_key = resolve_run_script_name_config_key(doc)
    last_name = config.get_config_str(name_config_key)
    names, merged_scripts, _unused_origin_map = picker_state
    # A raw document name from an older list still selects that script when
    # the picker key is the [Doc] display name and no user script uses the raw name.
    if last_name and last_name not in merged_scripts:
        display = document_script_display_name(last_name)
        if display in merged_scripts:
            last_name = display
    if not last_name or last_name not in merged_scripts:
        if names:
            last_name = names[0]
        else:
            last_name = ""
        if last_name:
            config.set_config(name_config_key, last_name)
    selected_code = merged_scripts.get(last_name, "")
    return last_name, selected_code, merged_scripts


def build_scripts_list_message(
    *,
    session_doc: Any | None,
    session_doc_url: str | None,
    status_ok_text: str | None = None,
    status_error_text: str | None = None,
) -> dict[str, Any]:
    user_scripts = get_user_scripts()

    doc = session_doc

    document_available = doc is not None
    document_stale = False
    document_readonly = is_document_readonly_for_scripts(doc)

    # The list and the writes both use the launch document;
    # None is not a document, so it is not stale and not writable.
    if document_scripts_write_is_stale(doc, session_doc_url):
        document_stale = True
        document_readonly = True

    loaded: dict[str, str] = {}
    load_err: DocumentScriptError | None = None
    doc_scripts: dict[str, str] = {}
    if doc is not None and not document_stale:
        # Same keys as build_xdl_script_picker_state. Raw names collided with
        # My Scripts and did not match selected_script_name ([Doc] …), so
        # reopen loaded the wrong row and Save wrote a different script.
        # scripts_manager.js documentScriptListKey must probe these keys for
        # the New / Save As overwrite check. The typed name is not a key.
        loaded, load_err = _load_document_script_map(doc)
        if load_err is None:
            doc_scripts = {
                document_script_display_name(name): code
                for name, code in picker_document_scripts(loaded).items()
            }
        else:
            if not status_ok_text and not status_error_text:
                status_error_text = str(load_err)

    sections: list[dict[str, Any]] = [
        {"id": SCRIPT_ORIGIN_USER, "title": _("My Scripts"), "scripts": user_scripts},
        {"id": SCRIPT_ORIGIN_DOCUMENT, "title": _("This Document"), "scripts": doc_scripts},
    ]

    domain_entries = domain_template_entries(doc)
    for domain, display_scripts in domain_entries:
        sections.append({
            "id": domain.origin,
            "title": domain.title_fn(),
            "scripts": display_scripts,
        })

    # A stale document must not be passed to resolve_run_script_selection.
    # That returned a '[Doc] X' selection missing from sections and wrote a
    # selection config entry against the stale doc. Pass None so selection
    # only considers user scripts and templates.
    selection_doc = None if document_stale or doc is None else doc

    # What was wrong: sections came from one property read and one
    # domain_template_entries(doc) call, then resolve_run_script_selection
    # called build_xdl_script_picker_state, which read the property again and
    # expanded templates again.
    # How: picker_state existed to skip that rebuild, and this caller did not
    # pass it.
    # Why this fixes it: the non-stale path reuses loaded and domain_entries.
    # The stale path still selects with {} and domain_template_entries(None),
    # not the section templates, so a template supported only by the stale
    # document is not stored as the selection.
    if selection_doc is None:
        picker_state = _picker_state_from_parts(
            user_scripts, {}, domain_template_entries(None)
        )
    else:
        doc_map = loaded if load_err is None else {}
        picker_state = _picker_state_from_parts(
            user_scripts, doc_map, domain_entries
        )

    selected_name, _selected_code, _merged_scripts = resolve_run_script_selection(
        selection_doc, picker_state
    )

    msg: dict[str, Any] = {
        "type": "scripts_list",
        "sections": sections,
        "document_available": document_available,
        "document_readonly": document_readonly,
        "document_stale": document_stale,
        "selected_script_name": selected_name,
    }
    if status_ok_text:
        msg["status_ok_text"] = status_ok_text
    if status_error_text:
        msg["status_error_text"] = status_error_text
    return msg


def _script_code_from_message(msg: dict[str, Any]) -> str:
    raw = msg.get("code", "")
    return raw if isinstance(raw, str) else ""


def _document_script_storage_name(name: str) -> str:
    """Property key for a picker label. ``[Doc] Regional`` stores as ``Regional``."""
    return parse_document_script_display_name(name) or name


def get_user_scripts() -> dict[str, str]:
    scripts = config.get_config("saved_python_scripts")
    if not isinstance(scripts, dict):
        return {}
    return dict(scripts)


def read_only_helper_message() -> str:
    return _("Built-in helpers are read-only. Use Save As to copy one into My Scripts.")


def readonly_fallback_saved_message(name: str, document_error: object) -> str:
    return _("Could not save '{0}' to this document ({1}). Saved it to My Scripts instead.").format(
        name, document_error
    )


def readonly_fallback_name_taken_message(name: str, document_error: object) -> str:
    return _("Could not save '{0}' to this document ({1}). A script named '{0}' already exists in My Scripts.").format(
        name, document_error
    )


def save_user_script(name: str, code: str) -> DocumentScriptError | None:
    from plugin.scripting.domain_registry import is_reserved_script_name

    # What was wrong: A reserved name raised ValueError. Monaco New does not
    # pre-check prefixes, and _on_save_script did not catch it, so
    # editor_host._handle_picker showed str(ValueError).
    # Why this change fixes it: attach_document_script already returns RESERVED_NAME.
    # Returning that error writes nothing and gives the picker a translated sentence.
    if is_reserved_script_name(name):
        return DocumentScriptError(
            _("'{0}' starts with a reserved prefix.").format(name),
            DocumentScriptErrorCode.RESERVED_NAME,
        )
    scripts = get_user_scripts()
    scripts[name] = code
    config.set_config("saved_python_scripts", scripts)
    return None


def delete_user_script(name: str) -> DocumentScriptError | None:
    scripts = get_user_scripts()
    # Deleting a user script that is not there must not report success or
    # rewrite config. Return NOT_FOUND and skip set_config.
    if name not in scripts:
        return DocumentScriptError(
            _("Script '{0}' does not exist in My Scripts.").format(name),
            DocumentScriptErrorCode.NOT_FOUND,
        )
    del scripts[name]
    config.set_config("saved_python_scripts", scripts)
    return None


def is_picker_template_name(name: str) -> bool:
    """True for a read-only built-in picker label such as ``[Vision] …``."""
    return any(parse_picker_display_name(domain.display_prefix, name) for domain in get_picker_domains())


def save_selected_script(
    doc: Any | None,
    display_name: str,
    code: str,
    *,
    allow_builtin_skip: bool = False,
    is_builtin_fn: Callable[[str], bool] | None = None,
) -> str | None:
    """Save *code* to user scripts or *doc* matching *display_name*.

    Returns None on success or when *display_name* is a built-in template and
    *allow_builtin_skip* is True. Returns an error message string otherwise.
    """
    if not display_name:
        return None
    user_scripts = get_user_scripts()
    if display_name in user_scripts:
        save_err = save_user_script(display_name, code)
        if isinstance(save_err, str) and save_err:
            return save_err
        return None
    if doc is not None:
        stored, load_err = _load_document_script_map(doc)
    else:
        stored, load_err = {}, None
    check_builtin = is_builtin_fn if is_builtin_fn is not None else is_picker_template_name
    if check_builtin(display_name):
        if allow_builtin_skip:
            return None
        return read_only_helper_message()
    if load_err is not None:
        return str(load_err)
    real_doc_name = parse_document_script_display_name(display_name) or display_name
    if real_doc_name in stored:
        return save_document_script(doc, real_doc_name, code)
    return _("Script '{0}' is not in My Scripts or this document, so it was not saved.").format(display_name)


def _closed_document_scripts_list(status_error_text: str) -> dict[str, Any]:
    """Script list when the document is already gone and cannot be read again."""
    # The second disposal happens while rebuilding the list. User scripts
    # are config and need no document. Template supports() can touch the
    # closed document, so they are omitted.
    return {
        "type": "scripts_list",
        "sections": [
            {"id": SCRIPT_ORIGIN_USER, "title": _("My Scripts"), "scripts": get_user_scripts()},
            {"id": SCRIPT_ORIGIN_DOCUMENT, "title": _("This Document"), "scripts": {}},
        ],
        "document_available": False,
        "document_readonly": True,
        "document_stale": True,
        "selected_script_name": "",
        "status_error_text": status_error_text,
    }


def _require_writable_session(
    session_doc: Any | None,
    session_doc_url: str | None,
    *,
    action_label: str = "",
) -> str | None:
    """Validate that the session document is open and not stale. Return error text or None."""
    if session_doc is None:
        if action_label == "save":
            return _("No document is open to save scripts.")
        return _("No document is open.")
    if document_scripts_write_is_stale(session_doc, session_doc_url):
        return _("Document changed — close and reopen Run Python Script to edit document scripts.")
    return None


def _remember_selection(session_doc: Any | None, name: str) -> None:
    name_config_key = resolve_run_script_name_config_key(session_doc)
    config.set_config(name_config_key, name)


def _on_request_scripts(
    msg: dict[str, Any],
    *,
    session_doc: Any | None,
    session_doc_url: str | None,
    send_list: Callable[..., None],
) -> bool:
    log.info("scripts picker: request_scripts")
    send_list()
    return True


def _on_select_script(
    msg: dict[str, Any],
    *,
    session_doc: Any | None,
    session_doc_url: str | None,
    send_list: Callable[..., None],
) -> bool:
    name = str(msg.get("name", "") or "").strip()
    _remember_selection(session_doc, name)
    return True


def _validate_script_target(
    msg: dict[str, Any],
    send_list: Callable[..., None],
    *,
    session_doc: Any | None = None,
    session_doc_url: str | None = None,
    action_label: str = "delete",
) -> tuple[str, str, str | None] | None:
    name = str(msg.get("name", "") or "").strip()
    origin = str(msg.get("origin", "") or "").strip()
    if not name:
        send_list(status_error_text=_("Script name cannot be empty."))
        return None
    if not script_origin_is_writable(origin):
        send_list(status_error_text=read_only_helper_message())
        return None
    storage_name: str | None = None
    if origin == SCRIPT_ORIGIN_DOCUMENT:
        err = _require_writable_session(session_doc, session_doc_url, action_label=action_label)
        if err:
            send_list(status_error_text=err)
            return None
        storage_name = _document_script_storage_name(name)
    return name, origin, storage_name


def _on_save_script(
    msg: dict[str, Any],
    *,
    session_doc: Any | None,
    session_doc_url: str | None,
    send_list: Callable[..., None],
) -> bool:
    target = _validate_script_target(
        msg,
        send_list,
        session_doc=session_doc,
        session_doc_url=session_doc_url,
        action_label="save",
    )
    if target is None:
        return True
    name, origin, storage_name = target
    script_code = _script_code_from_message(msg)
    if origin == SCRIPT_ORIGIN_DOCUMENT:
        assert storage_name is not None
        save_err = save_document_script(session_doc, storage_name, script_code)
        if save_err:
            # Fall back to My Scripts only for READONLY (read-only document or
            # unwritable properties). Other save errors, including 'INIT' and
            # too-large, go back to the user. A blanket fallback skipped validation.
            if getattr(save_err, "code", None) == DocumentScriptErrorCode.READONLY:
                if storage_name in get_user_scripts():
                    send_list(
                        status_error_text=readonly_fallback_name_taken_message(storage_name, save_err)
                    )
                    return True
                user_err = save_user_script(storage_name, script_code)
                if user_err:
                    send_list(status_error_text=str(user_err))
                    return True
                _remember_selection(session_doc, storage_name)
                send_list(
                    status_ok_text=readonly_fallback_saved_message(storage_name, save_err),
                )
                return True
            send_list(status_error_text=str(save_err))
            return True
        remembered = document_script_display_name(storage_name)
        ok_text = _("Saved script '{0}' to this document.").format(storage_name)
        log_user = False
    else:
        user_err = save_user_script(name, script_code)
        if user_err:
            send_list(status_error_text=str(user_err))
            return True
        remembered = name
        ok_text = _("Saved script '{0}'.").format(name)
        log_user = True
    _remember_selection(session_doc, remembered)
    if log_user:
        log.info("scripts picker: save_script '%s' (user)", name)
    send_list(status_ok_text=ok_text)
    return True


def _on_delete_script(
    msg: dict[str, Any],
    *,
    session_doc: Any | None,
    session_doc_url: str | None,
    send_list: Callable[..., None],
) -> bool:
    target = _validate_script_target(msg, send_list, session_doc=session_doc, session_doc_url=session_doc_url)
    if target is None:
        return True
    name, origin, storage_name = target
    if origin == SCRIPT_ORIGIN_DOCUMENT:
        assert storage_name is not None
        del_err = delete_document_script(session_doc, storage_name)
        if del_err:
            send_list(status_error_text=str(del_err))
            return True
        send_list(status_ok_text=_("Deleted document script '{0}'.").format(storage_name))
        return True
    del_err = delete_user_script(name)
    if del_err:
        send_list(status_error_text=str(del_err))
        return True
    log.info("scripts picker: delete_script '%s' (user)", name)
    send_list(status_ok_text=_("Deleted script '{0}'.").format(name))
    return True


_PICKER_HANDLERS: dict[str, Callable[..., bool]] = {
    "request_scripts": _on_request_scripts,
    "select_script": _on_select_script,
    "save_script": _on_save_script,
    "delete_script": _on_delete_script,
}

# editor_host._dispatch_incoming routes on this set. It is the handler
# map's keys so a new picker message cannot be handled here and dropped there.
SCRIPT_PICKER_MESSAGE_TYPES = frozenset(_PICKER_HANDLERS)


def handle_editor_script_message(
    kind: str,
    msg: dict[str, Any],
    *,
    session_doc: Any | None,
    session_doc_url: str | None,
    send: Callable[[dict[str, Any]], None],
) -> bool:
    """Apply a Monaco script-picker IPC message. Return True if *kind* was handled."""

    def _send_list(*, status_ok_text: str | None = None, status_error_text: str | None = None) -> None:
        send(
            build_scripts_list_message(
                session_doc=session_doc,
                session_doc_url=session_doc_url,
                status_ok_text=status_ok_text,
                status_error_text=status_error_text,
            )
        )

    handler = _PICKER_HANDLERS.get(kind)
    if handler is None:
        return False
    try:
        return handler(
            msg,
            session_doc=session_doc,
            session_doc_url=session_doc_url,
            send_list=_send_list,
        )
    except DocumentDisposedError:
        # Known handlers and set_document_scripts re-raise DocumentDisposedError.
        # An unknown kind never enters this except because the handler lookup
        # returns False first. A second disposal uses _closed_document_scripts_list,
        # which does not touch the document.
        log.exception("scripts picker: document disposed during %s", kind)
        closed = _("The document was closed.")
        try:
            _send_list(status_error_text=closed)
        except DocumentDisposedError:
            log.exception("scripts picker: list refresh failed after the document closed")
            send(_closed_document_scripts_list(closed))
        return True

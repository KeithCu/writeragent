# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Client / LLM-wire specific error helpers.

This module is intentionally a thin adapter + specialized helpers after the
2026 framework error formatting centralization:

- format_error_message is now re-exported from (and implemented in)
  plugin.framework.errors as the single i18n mapper for the whole codebase.
- _format_http_error_response and is_audio_unsupported_error stay here
  because they are tightly coupled to HTTP response bodies and LLM modality
  detection (wire concerns that do not belong in the core framework layer).
- format_error_for_display is a convenience that already delegated to the
  central payload formatter; it remains here for the public client API surface.

All call sites inside client/ (llm_client, requests) continue to import from
here so the public names and re-exports in client/__init__.py are unchanged.
"""

from plugin.framework.i18n import _

# Re-export the single central i18n mapper so existing imports from
# plugin.framework.client.errors (and the re-exports in client/__init__.py)
# keep working without any behavior change.
from plugin.framework.errors import format_error_message  # noqa: F401

_ZAI_CODING_PLAN_ENDPOINT = "https://api.z.ai/api/coding/paas/v4"


def _format_http_error_response(status, reason, err_body):
    """Build error message including response body for display in chat/UI.

    This remains client-specific because it parses provider error JSON bodies
    and falls back to raw snippets — behavior that is only relevant on the
    LLM HTTP path.
    """
    base = _("HTTP Error {0} from AI Provider: {1}").format(status, reason)
    if not err_body or not err_body.strip():
        return base
    from plugin.framework.errors import safe_json_loads

    data = safe_json_loads(err_body)
    if data is not None and isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            detail = err.get("message") or err.get("msg") or err.get("error") or ""
        else:
            detail = str(err) if err else ""
        if detail:
            # Together and some providers return error.message as a dict, not a string.
            if not isinstance(detail, str):
                detail = str(detail)
            return base + ". " + detail
    snippet = err_body.strip().replace("\n", " ")[:400]
    return base + ".\nProvider Response:\n" + snippet


def append_zai_unknown_model_hint(message, err_body, path, provider, request_model=None):
    """Append Coding Plan endpoint guidance when Z.ai returns unknown-model 400."""
    if (provider or "").lower() != "zai":
        return message
    path_l = str(path or "").lower()
    if "/api/coding/" in path_l:
        return message
    err_l = str(err_body or "").lower()
    if "unknown model" not in err_l and '"code":"1211"' not in err_l and '"code": "1211"' not in err_l:
        return message
    hint = _(
        " If your API key is from a GLM Coding Plan subscription, set endpoint to "
        "{0} (not the general /api/paas URL)."
    ).format(_ZAI_CODING_PLAN_ENDPOINT)
    if request_model:
        return message + hint + _(" Request model was: {0}.").format(repr(request_model))
    return message + hint


def format_error_for_display(e):
    """Return user-friendly error string for display in cells or dialogs."""
    from plugin.framework.errors import format_error_payload

    payload = format_error_payload(e)
    return _("Error: {0}").format(payload.get("message", format_error_message(e)))


def is_audio_unsupported_error(e):
    """Try to determine if the error indicates that audio/modality is unsupported by the model."""
    msg = str(e).lower()

    # Common error strings across providers
    if "unsupported content type" in msg:
        return True
    if "unsupported modality" in msg:
        return True
    if "audio" in msg and ("not supported" in msg or "unsupported" in msg):
        return True
    if "modality" in msg and "not supported" in msg:
        return True

    # Specific API error bodies (passed via _format_http_error_response)
    if "model" in msg and "cannot process" in msg and "audio" in msg:
        return True
    if "no endpoints found that support input audio" in msg:
        return True
    if "gpt-4" in msg and "audio" in msg:  # Some legacy GPT-4 might not have it
        if "not support" in msg:
            return True

    return False

import json
import ssl
import socket
import http.client


def format_error_message(e):
    """Map common exceptions to user-friendly advice."""
    import urllib.error

    msg = str(e)
    if isinstance(e, ssl.SSLError):
        return "TLS/SSL Error: %s" % msg
    if isinstance(e, (urllib.error.HTTPError, http.client.HTTPResponse)):
        code = e.code if hasattr(e, "code") else e.status
        reason = e.reason if hasattr(e, "reason") else ""
        if code == 401:
            return "Invalid API Key. Please check your settings."
        if code == 403:
            return "API access Forbidden. Your key may lack permissions for this model."
        if code == 404:
            return "Endpoint not found (404). Check your URL and Model name."
        if code >= 500:
            return "Server error (%d). The AI provider is having issues." % code
        return "HTTP Error %d: %s" % (code, reason)

    if isinstance(e, socket.timeout) or "timed out" in msg.lower():
        return "Request Timed Out. Try increasing 'Request Timeout' in Settings."

    if isinstance(e, (urllib.error.URLError, socket.error)):
        reason = str(e.reason) if hasattr(e, "reason") else str(e)
        if "Connection refused" in reason or "111" in reason:
            return "Connection Refused. Is your local AI server (Ollama/LM Studio) running?"
        if "getaddrinfo failed" in reason:
            return "DNS Error. Could not resolve the endpoint URL."
        return "Connection Error: %s" % reason

    if "finish_reason=error" in msg:
        return "The AI provider reported an error. Try again."

    return msg


def _format_http_error_response(status, reason, err_body):
    """Build error message including response body for display in chat/UI."""
    base = "HTTP Error %d: %s" % (status, reason)
    if not err_body or not err_body.strip():
        return base
    try:
        data = json.loads(err_body)
        err = data.get("error")
        if isinstance(err, dict):
            detail = err.get("message") or err.get("msg") or err.get("error") or ""
        else:
            detail = str(err) if err else ""
        if detail:
            return base + ". " + detail
    except (json.JSONDecodeError, TypeError):
        pass
    snippet = err_body.strip().replace("\n", " ")[:400]
    return base + ". " + snippet


def format_error_for_display(e):
    """Return user-friendly error string for display in cells or dialogs."""
    from plugin.framework.errors import format_error_payload
    payload = format_error_payload(e)
    return "Error: %s" % payload.get("message", format_error_message(e))


def is_audio_unsupported_error(e):
    """Try to determine if the error indicates that audio/modality is unsupported by the model."""
    msg = str(e).lower()

    # Common error strings across providers
    if "unsupported content type" in msg: return True
    if "unsupported modality" in msg: return True
    if "audio" in msg and ("not supported" in msg or "unsupported" in msg): return True
    if "modality" in msg and "not supported" in msg: return True

    # Specific API error bodies (passed via _format_http_error_response)
    if "model" in msg and "cannot process" in msg and "audio" in msg: return True
    if "no endpoints found that support input audio" in msg: return True
    if "gpt-4" in msg and "audio" in msg: # Some legacy GPT-4 might not have it
        if "not support" in msg: return True

    return False

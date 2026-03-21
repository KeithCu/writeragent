import json
import logging
import urllib.request
import urllib.parse
from plugin.framework.constants import APP_REFERER, APP_TITLE, USER_AGENT
from plugin.framework.errors import NetworkError
from plugin.framework.utils import get_url_hostname
from plugin.modules.http.ssl_helpers import get_verified_ssl_context, get_unverified_ssl_context, _is_local_host, _is_certificate_verify_error
from plugin.modules.http.errors import _format_http_error_response, format_error_message

log = logging.getLogger(__name__)

def sync_request(url, data=None, headers=None, timeout=10, parse_json=True):
    """
    Blocking HTTP GET or POST. Shared by aihordeclient and other code.
    url: str or urllib.request.Request. If Request, headers/data come from it.
    data: optional bytes for POST. headers: optional dict (used only if url is str).
    Returns response data: decoded JSON if parse_json else raw bytes. Raises on error.
    """
    import urllib.error
    if headers is None:
        headers = {}

    # Add default headers to avoid being blocked and provide application identity
    has_ua = any(k.lower() == "user-agent" for k in headers.keys())
    if not has_ua:
        headers["User-Agent"] = USER_AGENT

    if "HTTP-Referer" not in headers:
        headers["HTTP-Referer"] = APP_REFERER
    if "X-Title" not in headers:
        headers["X-Title"] = APP_TITLE

    if isinstance(url, str):
        req = urllib.request.Request(url, data=data, headers=headers)
    else:
        req = url

    # Debug: log which headers we are actually sending (keys only)
    try:
        header_keys = list(req.headers.keys()) if hasattr(req, "headers") else []
        if not header_keys and hasattr(req, "get_full_url"):
            # If it's a urllib Request object, headers might be in .headers
            pass
        log.debug(f"Request to {getattr(req, 'full_url', url)} with header keys: {header_keys}")
    except Exception:
        pass

    full_url = getattr(req, "full_url", url)
    parsed = urllib.parse.urlparse(str(full_url))
    host = get_url_hostname(str(full_url))
    is_local_https = parsed.scheme.lower() == "https" and _is_local_host(host)
    def _read_with_context(context):
        log.debug(f"About to open URL: {getattr(req, 'full_url', url)}")
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            log.debug(f"URL opened, status={resp.getcode()}. Heading to read...")
            raw = resp.read()
            log.debug(f"Read {len(raw)} bytes")
            if parse_json:
                return json.loads(raw.decode("utf-8"))
            return raw

    ctx = get_verified_ssl_context() if is_local_https else get_unverified_ssl_context()
    try:
        return _read_with_context(ctx)
    except urllib.error.HTTPError as e:
        status = e.code
        reason = e.reason
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""

        msg = _format_http_error_response(status, reason, err_body)
        log.error(f"HTTP Error: {msg}")
        raise NetworkError(msg, code="HTTP_ERROR", context={"url": url, "status": status}) from e
    except NetworkError:
        raise
    except Exception as e:
        if is_local_https and _is_certificate_verify_error(e):
            log.error("Local HTTPS certificate verification failed for %s; retrying unverified." % host)
            try:
                return _read_with_context(get_unverified_ssl_context())
            except urllib.error.HTTPError as retry_http_e:
                status = retry_http_e.code
                reason = retry_http_e.reason
                try:
                    err_body = retry_http_e.read().decode("utf-8", errors="replace")
                except Exception:
                    err_body = ""
                msg = _format_http_error_response(status, reason, err_body)
                log.error(f"HTTP Error: {msg}")
                raise NetworkError(msg, code="HTTP_ERROR", context={"url": url, "status": status}) from retry_http_e
            except Exception as retry_e:
                log.error(f"Request failed: {format_error_message(retry_e)}")
                raise NetworkError(format_error_message(retry_e), context={"url": url}) from retry_e
        log.error(f"Request failed: {format_error_message(e)}")
        raise NetworkError(format_error_message(e), context={"url": url}) from e

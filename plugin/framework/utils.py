import os
import urllib.parse

def get_plugin_dir():
    """Returns the absolute path to the plugin/ directory."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def normalize_endpoint_url(url):
    """Strip whitespace, trailing slashes, and a trailing ``/v1`` segment for consistent storage.

    OpenAI-compatible clients append ``/v1/...``; bases pasted with ``/v1`` would otherwise
    become ``/v1/v1/...``. Paths like ``.../v1beta/...`` are unchanged (they do not end with ``/v1``).
    """
    if not url or not isinstance(url, str):
        return ""
    ep = url.strip().rstrip("/")
    if ep.lower().endswith("/v1"):
        ep = ep[:-3].rstrip("/")
    return ep

def get_url_hostname(url):
    """Extract hostname safely."""
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.hostname or ""
    except ValueError:
        return ""

def get_url_domain(url):
    """Extract domain (like netloc without www, for logging)."""
    try:
        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc or str(url)[:30]
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except ValueError:
        return str(url)[:30]

def get_url_path(url):
    """Extract path safely."""
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.path
    except ValueError:
        return ""

def get_url_query_dict(url):
    """Extract query string as dictionary safely."""
    try:
        parsed = urllib.parse.urlparse(url)
        return urllib.parse.parse_qs(parsed.query)
    except ValueError:
        return {}

def get_url_path_and_query(url):
    """Reconstruct path + query efficiently."""
    try:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        if parsed.query:
            path += "?" + parsed.query
        return path
    except ValueError:
        return ""

def is_pdf_url(url):
    """Check for .pdf in the URL path safely."""
    try:
        parsed = urllib.parse.urlparse(url)
        return (parsed.path or "").lower().endswith(".pdf")
    except ValueError:
        return False

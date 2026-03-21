import os
import urllib.parse

def get_plugin_dir():
    """Returns the absolute path to the plugin/ directory."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def normalize_endpoint_url(url):
    """Strip and rstrip slash for consistent storage."""
    if not url or not isinstance(url, str):
        return ""
    return url.strip().rstrip("/")

def get_url_hostname(url):
    """Extract hostname safely."""
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.hostname or ""
    except Exception:
        return ""

def get_url_domain(url):
    """Extract domain (like netloc without www, for logging)."""
    try:
        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc or str(url)[:30]
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return str(url)[:30]

def get_url_path(url):
    """Extract path safely."""
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.path
    except Exception:
        return ""

def get_url_query_dict(url):
    """Extract query string as dictionary safely."""
    try:
        parsed = urllib.parse.urlparse(url)
        return urllib.parse.parse_qs(parsed.query)
    except Exception:
        return {}

def get_url_path_and_query(url):
    """Reconstruct path + query efficiently."""
    try:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        if parsed.query:
            path += "?" + parsed.query
        return path
    except Exception:
        return ""

def is_pdf_url(url):
    """Check for .pdf in the URL path safely."""
    try:
        parsed = urllib.parse.urlparse(url)
        return (parsed.path or "").lower().endswith(".pdf")
    except Exception:
        return False

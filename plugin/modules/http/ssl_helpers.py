import ssl
import ipaddress


def get_unverified_ssl_context():
    """Create an SSL context that doesn't verify certificates. Shared by API and aihordeclient."""
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    return ssl_context


def get_verified_ssl_context():
    """Create a default verifying SSL context."""
    return ssl.create_default_context()


def _is_certificate_verify_error(e):
    """Return True when an exception points to certificate validation failure."""
    if isinstance(e, ssl.SSLCertVerificationError):
        return True
    reason = getattr(e, "reason", None)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return True
    msg = ("%s %s" % (e, reason or "")).lower()
    for marker in ("certificate_verify_failed", "certificate verify failed", "self-signed certificate", "self signed certificate", "unable to get local issuer certificate", "hostname mismatch"):
        if marker in msg:
            return True
    return False


def _is_local_host(host):
    """Heuristic for localhost / LAN hosts where self-signed TLS is common."""
    host = (host or "").strip().lower()
    if not host:
        return False
    if host in ("localhost", "ip6-localhost", "host.docker.internal"):
        return True
    if host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        pass
    # Single-label hostnames are usually local network names.
    return "." not in host

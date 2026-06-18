# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

import ssl


def get_unverified_ssl_context():
    """Create an SSL context that doesn't verify certificates. Shared by API clients."""
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


# Re-export the canonical implementation after the 2026 provider detection consolidation.
# New code should prefer `from plugin.framework.client.provider_detection import is_local_host`.
from plugin.framework.client.provider_detection import is_local_host as _is_local_host  # noqa: F401 - re-export under old private name for compat

# Keep the old private name for existing internal callers (llm_client, requests)
# so we don't have to touch every import site in this first-pass cleanup.
# The real logic now lives in provider_detection.py.

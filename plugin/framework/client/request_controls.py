# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Low-level pacing and retry policy helpers for outbound LLM requests."""

import logging
import time
from typing import Callable

from .ssl_helpers import _is_certificate_verify_error, _is_local_host

log = logging.getLogger(__name__)

# Minimum wall time between consecutive HTTP sends on one client. Grammar queue
# workers also use this value to stagger parallel drains so they do not burst.
LLM_MIN_REQUEST_INTERVAL_SEC = 0.05


class RequestPacer:
    """Sleep before rapid repeat sends on the same client."""

    def __init__(self, min_interval_sec: float = LLM_MIN_REQUEST_INTERVAL_SEC, *, monotonic: Callable[[], float] | None = None, sleep: Callable[[float], None] | None = None) -> None:
        self.min_interval_sec = min_interval_sec
        self._monotonic = monotonic
        self._sleep = sleep
        self.last_sent_monotonic = 0.0

    def _now(self) -> float:
        return (self._monotonic or time.monotonic)()

    def wait_before_send(self) -> None:
        """Sleep if needed so consecutive sends are not back-to-back."""
        wait = self.min_interval_sec - (self._now() - self.last_sent_monotonic)
        if wait > 0:
            (self._sleep or time.sleep)(wait)

    def mark_sent(self) -> None:
        self.last_sent_monotonic = self._now()


class LocalHttpsCertificateFallback:
    """Track local HTTPS hosts that should retry with certificate verification disabled."""

    def __init__(self) -> None:
        self._fallback_hosts: set[str] = set()

    def ssl_mode_for(self, scheme: str, host: str) -> str:
        """Return ``verified``, ``unverified``, or ``plain`` for the next connection."""
        if scheme != "https":
            return "plain"
        if _is_local_host(host) and host not in self._fallback_hosts:
            return "verified"
        return "unverified"

    def enable_if_applicable(self, host: str, err: BaseException) -> bool:
        """Enable unverified retry for a local host after certificate validation fails."""
        if not host or not _is_local_host(host) or not _is_certificate_verify_error(err):
            return False
        if host in self._fallback_hosts:
            return False
        self._fallback_hosts.add(host)
        log.error("Local HTTPS certificate verification failed for %s; retrying unverified." % host)
        return True

    def has_fallback(self, host: str) -> bool:
        return host in self._fallback_hosts

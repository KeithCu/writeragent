# WriterAgent - Tests for Python Compute Service
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared fixtures and utilities for compute service tests."""

from __future__ import annotations

import socket


def get_free_port() -> int:
    """Return an available loopback port, attempting IPv6 first then IPv4."""
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            return s.getsockname()[1]
    except OSError:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            return s.getsockname()[1]

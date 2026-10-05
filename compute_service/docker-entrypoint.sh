#!/bin/sh
# WriterAgent - Python Compute Service image entrypoint
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
#
# The image binds every interface. A plain `docker run` used to start with
# no API key. Loopback without a key stays allowed for local dev; that path
# does not go through this script.
set -eu
host="${PYTHON_COMPUTE_HOST:-127.0.0.1}"
if [ "$host" = "0.0.0.0" ] || [ "$host" = "::" ]; then
  if [ -z "${PYTHON_COMPUTE_API_KEY:-}" ] && [ -z "${PYTHON_COMPUTE_API_KEY_FILE:-}" ]; then
    echo "Refusing to listen on ${host} without PYTHON_COMPUTE_API_KEY or PYTHON_COMPUTE_API_KEY_FILE." >&2
    exit 1
  fi
fi
exec python compute_service/server.py "$@"

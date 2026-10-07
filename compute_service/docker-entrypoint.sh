#!/bin/sh
# WriterAgent - Python Compute Service image entrypoint
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
#
# The image binds every interface. A plain `docker run` used to start with
# no API key. Loopback without a key stays allowed for local dev; that path
# does not go through this script.
# ComputeSettings.validate enforces that binding to public interfaces
# (0.0.0.0 / ::) requires an API key.
set -eu
exec python compute_service/server.py "$@"
